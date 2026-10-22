"""Explicit, checkpointed NL2SQL graph."""

import json
import math
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from security import validate_sql
from knowledge import KnowledgeCatalog


class State(TypedDict, total=False):
    question: str
    rewritten: str
    clarification: str
    schema: str
    sql: str
    error: str
    status: str
    retry_count: int
    result: dict
    explanation: str
    warnings: list
    trace: list
    knowledge_version: str
    sources: list
    metrics: list




class Audit:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, thread_id TEXT, node TEXT, status TEXT, detail TEXT, created_at REAL)")

    def write(self, thread_id, node, status, detail=""):
        with closing(sqlite3.connect(self.path, timeout=10)) as conn, conn:
            conn.execute("INSERT INTO events(thread_id,node,status,detail,created_at) VALUES (?,?,?,?,?)", (thread_id, node, status, detail[:4000], time.time()))


class Workflow:
    def __init__(self, executor, planner, directory, max_retry=2, catalog=None):
        self.executor, self.planner = executor, planner
        self.catalog = catalog or KnowledgeCatalog(executor.path, directory / "knowledge", executor)
        self.max_retry = max_retry
        self.audit = Audit(directory / "audit.sqlite")
        self.connection = sqlite3.connect(directory / "checkpoints.sqlite", check_same_thread=False)
        self.lock = threading.Lock()
        graph = StateGraph(State)
        for name in ["rewrite_query", "ask_user", "retrieve_schema", "generate_sql", "sql_security_validate", "execute_sql", "fix_sql", "result_validate", "explain_result", "final_answer"]:
            graph.add_node(name, self.wrap(name, getattr(self, name)))
        graph.add_edge(START, "rewrite_query")
        graph.add_conditional_edges("rewrite_query", lambda s: "final_answer" if s.get("error") else "ask_user" if s.get("clarification") else "retrieve_schema")
        graph.add_edge("ask_user", "rewrite_query")
        graph.add_conditional_edges("retrieve_schema", lambda s: "final_answer" if s.get("error") else "generate_sql")
        graph.add_conditional_edges("generate_sql", lambda s: "final_answer" if s.get("error") else "ask_user" if s.get("clarification") else "sql_security_validate")
        graph.add_conditional_edges("sql_security_validate", lambda s: "final_answer" if s.get("error") else "execute_sql")
        graph.add_conditional_edges("execute_sql", self.after_execution)
        graph.add_conditional_edges("result_validate", lambda s: self.retry_route(s) if s.get("error") else "explain_result")
        graph.add_edge("fix_sql", "sql_security_validate")
        graph.add_edge("explain_result", "final_answer")
        graph.add_edge("final_answer", END)
        self.graph = graph.compile(checkpointer=SqliteSaver(self.connection))

    def wrap(self, name, function):
        def node(state, config):
            thread_id = config["configurable"]["thread_id"]
            self.audit.write(thread_id, name, "started")
            # interrupt raises a graph control signal and must not be caught here.
            try:
                update = function(state)
            except (ValueError, sqlite3.Error, OSError) as exc:
                update = {"error": str(exc), "status": "failed"}
            detail = update.get("error", "")
            if name == "retrieve_schema" and not detail:
                detail = json.dumps({"knowledge_version": update.get("knowledge_version"), "sources": update.get("sources")}, ensure_ascii=False)
            self.audit.write(thread_id, name, update.get("status", "completed"), detail)
            return {**update, "trace": state.get("trace", []) + [name]}
        return node

    def rewrite_query(self, state):
        return {**self.planner.rewrite(state["question"]), "error": ""}

    def ask_user(self, state):
        answer = interrupt({"clarification": state["clarification"]})
        return {"question": state["question"] + "\n用户补充：" + answer, "clarification": "", "error": ""}

    def retrieve_schema(self, state):
        retrieved = self.catalog.retrieve(state.get("rewritten", state["question"]))
        return {"schema": retrieved["context"], "knowledge_version": retrieved["version"], "sources": retrieved["sources"], "metrics": retrieved["metrics"]}

    def generate_sql(self, state):
        plan = self.planner.generate(state.get("rewritten", state["question"]), state["schema"])
        return {"sql": plan.get("sql", ""), "clarification": plan.get("clarification", ""), "error": ""}

    def sql_security_validate(self, state):
        # A model repair failure is terminal; do not execute the previous SQL again.
        if state.get("error"):
            return {}
        try:
            validate_sql(state["sql"])
            return {"error": ""}
        except ValueError as exc:
            return {"error": str(exc), "status": "rejected"}

    def execute_sql(self, state):
        try:
            return {"result": self.executor.execute(state["sql"]), "error": ""}
        except sqlite3.Error as exc:
            return {"error": "数据库执行错误：" + str(exc), "status": "retrying"}

    def retry_route(self, state):
        return "fix_sql" if state.get("retry_count", 0) < self.max_retry else "final_answer"

    def after_execution(self, state):
        return self.retry_route(state) if state.get("error") else "result_validate"

    def fix_sql(self, state):
        plan = self.planner.fix(state["question"], state["schema"], state["sql"], state["error"])
        return {"sql": plan.get("sql", ""), "retry_count": state.get("retry_count", 0) + 1, "error": "", "status": "retrying"}

    def result_validate(self, state):
        result = state["result"]
        warnings = []
        if not result["rows"]:
            warnings.append("没有符合条件的数据；空结果不自动认定为 SQL 错误。")
        for row in result["rows"]:
            for value in row:
                if isinstance(value, float) and not math.isfinite(value):
                    return {"error": "结果包含非有限数值，请检查计算表达式"}
                if isinstance(value, (float, int)) and abs(value) > 1e12:
                    warnings.append("结果存在超过 1 万亿的数值，请核对单位与关联关系。")
        error = self.catalog.validate_metrics(state["sql"], state.get("metrics", []))
        if error:
            return {"error": error}
        return {"warnings": list(dict.fromkeys(warnings)), "error": ""}

    def explain_result(self, state):
        try:
            explanation = self.planner.explain(state["question"], state["result"])
        except ValueError:
            explanation = f"查询返回 {len(state['result']['rows'])} 行；模型解读不可用，请查看表格与 SQL。"
        return {"explanation": explanation, "status": "completed"}

    def final_answer(self, state):
        return {"status": ("rejected" if state.get("status") == "rejected" else "failed") if state.get("error") else "completed"}

    def ask(self, question, thread_id=None):
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 2000:
            raise ValueError("请输入 1 至 2000 字的问题")
        started = time.monotonic()
        with self.lock:
            if thread_id is not None:
                if not isinstance(thread_id, str) or not re.fullmatch(r"[a-f0-9]{32}", thread_id):
                    raise ValueError("会话编号无效")
                config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}
                snapshot = self.graph.get_state(config)
                if not snapshot.next or not any(task.interrupts for task in snapshot.tasks):
                    raise ValueError("会话不存在、已结束或不处于追问中，请开始新查询")
                input_state = Command(resume=question.strip())
            else:
                thread_id = uuid.uuid4().hex
                config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}
                input_state = {"question": question.strip(), "retry_count": 0, "trace": [], "error": "", "status": "running"}
            output = self.graph.invoke(input_state, config)
            if output.get("__interrupt__"):
                self.audit.write(thread_id, "ask_user", "interrupted")
                return {"thread_id": thread_id, "status": "interrupted", "clarification": output["__interrupt__"][0].value["clarification"], "trace": output.get("trace", [])}
            return self.format_result(output, thread_id, round((time.monotonic()-started)*1000))

    @staticmethod
    def format_result(output, thread_id, elapsed_ms=0):
        result = output.get("result", {}) if not output.get("error") else {}
        return {**result, "thread_id": thread_id, "status": output["status"], "error": output.get("error", ""), "sql": output.get("sql", ""), "explanation": output.get("explanation", ""), "warnings": output.get("warnings", []), "retry_count": output["retry_count"], "trace": output.get("trace", []), "elapsed_ms": elapsed_ms, "definition": "；".join(m["definition"] for m in output.get("metrics", [])) or "未命中已审核指标；请核对 SQL 与业务口径。", "knowledge_version": output.get("knowledge_version", ""), "sources": output.get("sources", [])}

    def get_status(self, thread_id):
        if not isinstance(thread_id, str) or not re.fullmatch(r"[a-f0-9]{32}", thread_id):
            raise ValueError("会话编号无效")
        with self.lock:
            snapshot = self.graph.get_state({"configurable": {"thread_id": thread_id}})
            if not snapshot.values:
                raise ValueError("会话不存在")
            for task in snapshot.tasks:
                if task.interrupts:
                    return {"thread_id": thread_id, "status": "interrupted", "clarification": task.interrupts[0].value["clarification"], "trace": snapshot.values.get("trace", [])}
            if snapshot.next:
                # No background execution is scheduled by this read operation.
                return {"thread_id": thread_id, "status": "incomplete", "error": "上次运行未完成且不处于用户追问中；请重新发起查询", "trace": snapshot.values.get("trace", [])}
            return self.format_result(snapshot.values, thread_id)

    def close(self):
        self.connection.close()
        self.executor.close()
