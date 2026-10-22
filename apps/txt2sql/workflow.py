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

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


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


def validate_sql(sql):
    if not isinstance(sql, str) or not 0 < len(sql) <= 20000:
        raise ValueError("未返回有效 SQL")
    try:
        statements = sqlglot.parse(sql, read="sqlite")
    except sqlglot.errors.ParseError as exc:
        raise ValueError("SQL 语法无法解析") from exc
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise ValueError("仅允许单条 SELECT 查询")
    tree = statements[0]
    banned = {"Insert", "Update", "Delete", "Create", "Drop", "Alter", "Command", "Pragma", "Attach", "Detach", "Into", "Transaction", "Lock", "Grant", "Copy", "Merge"}
    for node in tree.walk():
        if type(node).__name__ in banned:
            raise ValueError("SQL 包含禁止的操作")
        if isinstance(node, exp.Table) and (node.catalog or node.db):
            raise ValueError("禁止跨库查询")
        if isinstance(node, exp.Anonymous) and node.name.lower() not in {"strftime"}:
            raise ValueError("SQL 包含未允许的函数")
    for scope in traverse_scope(tree):
        for source in scope.sources.values():
            if isinstance(source, exp.Table) and source.name.lower() != "orders":
                raise ValueError("只允许访问已授权的 orders 表")
    return tree


class Catalog:
    """Small, local lexical RAG index over reviewed schema/metric/example documents."""
    def __init__(self, context, examples):
        self.documents = [
            ("table", "orders 订单表：id, order_date, region, product, amount, status。"),
            ("fields", "日期 order_date；地区 region；产品 product；金额 amount（元）；状态 status（已完成/已退款）。"),
            ("metric", "销售额口径：SUM(amount)，必须仅统计 status='已完成'，排除退款。"),
            ("range", "合成数据时间范围 2026-01 至 2026-06；未指定时间时统计全部演示数据。"),
        ] + [("example", q + "\n" + sql) for q, sql in examples.items()]
        self.context = context

    @staticmethod
    def terms(text):
        return set(re.findall(r"[a-z_]+|[\u4e00-\u9fff]{1,2}", text.lower()))

    def retrieve(self, question):
        terms = self.terms(question)
        ranked = sorted(self.documents, key=lambda doc: len(terms & self.terms(doc[1])), reverse=True)
        # Core schema and metric definitions are mandatory, even if lexical scores are zero.
        selected = self.documents[:4] + [doc for doc in ranked if doc[0] == "example"][:2]
        return self.context + "\n检索参考：\n" + "\n".join(text for _, text in selected)


class Audit:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, thread_id TEXT, node TEXT, status TEXT, detail TEXT, created_at REAL)")

    def write(self, thread_id, node, status, detail=""):
        with closing(sqlite3.connect(self.path, timeout=10)) as conn, conn:
            conn.execute("INSERT INTO events(thread_id,node,status,detail,created_at) VALUES (?,?,?,?,?)", (thread_id, node, status, detail[:4000], time.time()))


class Workflow:
    def __init__(self, executor, planner, directory, context, examples, max_retry=2):
        self.executor, self.planner = executor, planner
        self.catalog = Catalog(context, examples)
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
        graph.add_edge("retrieve_schema", "generate_sql")
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
            except (ValueError, sqlite3.Error) as exc:
                update = {"error": str(exc), "status": "failed"}
            self.audit.write(thread_id, name, update.get("status", "completed"), update.get("error", ""))
            return {**update, "trace": state.get("trace", []) + [name]}
        return node

    def rewrite_query(self, state):
        return {**self.planner.rewrite(state["question"]), "error": ""}

    def ask_user(self, state):
        answer = interrupt({"clarification": state["clarification"]})
        return {"question": state["question"] + "\n用户补充：" + answer, "clarification": "", "error": ""}

    def retrieve_schema(self, state):
        return {"schema": self.catalog.retrieve(state.get("rewritten", state["question"]))}

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
        if "销售额" in state["question"]:
            tree = validate_sql(state["sql"])
            # Validate direct source reads; subqueries/joins need stronger semantic proofs.
            tables = list(tree.find_all(exp.Table))
            selects = list(tree.find_all(exp.Select))
            def conjuncts(node):
                if isinstance(node, exp.And):
                    return conjuncts(node.this) + conjuncts(node.expression)
                if isinstance(node, exp.Paren):
                    return conjuncts(node.this)
                return [node]
            completed = any(isinstance(eq, exp.EQ) and isinstance(eq.this, exp.Column) and eq.this.name.lower() == "status" and isinstance(eq.expression, exp.Literal) and eq.expression.this == "已完成" for select in selects if select.args.get("where") for eq in conjuncts(select.args["where"].this))
            if len(tables) != 1 or len(selects) != 1 or not completed:
                return {"error": "销售额需使用单表查询并在 WHERE 中限定 status='已完成'；复杂口径需人工确认"}
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
                if not snapshot.next:
                    raise ValueError("会话不存在或已结束，请开始新查询")
                input_state = Command(resume=question.strip())
            else:
                thread_id = uuid.uuid4().hex
                config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}
                input_state = {"question": question.strip(), "retry_count": 0, "trace": [], "error": "", "status": "running"}
            output = self.graph.invoke(input_state, config)
            if output.get("__interrupt__"):
                self.audit.write(thread_id, "ask_user", "interrupted")
                return {"thread_id": thread_id, "status": "interrupted", "clarification": output["__interrupt__"][0].value["clarification"], "trace": output.get("trace", [])}
            result = output.get("result", {}) if not output.get("error") else {}
            return {**result, "thread_id": thread_id, "status": output["status"], "error": output.get("error", ""), "sql": output.get("sql", ""), "explanation": output.get("explanation", ""), "warnings": output.get("warnings", []), "retry_count": output["retry_count"], "trace": output.get("trace", []), "elapsed_ms": round((time.monotonic()-started)*1000), "definition": "销售额仅包含已完成订单，单位：元；合成数据范围 2026-01 至 2026-06。"}

    def close(self):
        self.connection.close()
        self.executor.close()
