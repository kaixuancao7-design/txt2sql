"""Authorized schema collection and explicit review of business knowledge."""

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from sqlglot import exp
from security import validate_sql

SEEDS = Path(__file__).with_name("knowledge") / "demo.json"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def collect_schema(database, allowed_tables=("orders",)):
    """Trusted metadata-only path, separate from the model SQL executor."""
    tables = []
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        for name in sorted(allowed_tables):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("授权表名格式不正确")
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone():
                raise ValueError("授权表不存在：" + name)
            columns = [{"name": r[1], "type": r[2], "not_null": bool(r[3]), "primary_key_position": r[5]} for r in conn.execute(f'PRAGMA table_info("{name}")')]
            foreign_keys = [{"table": r[2], "from": r[3], "to": r[4]} for r in conn.execute(f'PRAGMA foreign_key_list("{name}")') if r[2] in allowed_tables]
            tables.append({"name": name, "columns": columns, "foreign_keys": foreign_keys})
    schema = {"dialect": "sqlite", "tables": tables}
    return {**schema, "fingerprint": digest(schema)}


class KnowledgeCatalog:
    def __init__(self, database, directory, executor):
        self.database, self.directory, self.executor = database, directory, executor
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def terms(text):
        text = text.lower()
        tokens = set(re.findall(r"[a-z_]+", text))
        for run in re.findall(r"[\u4e00-\u9fff]+", text):
            tokens.update(run)
            tokens.update(run[i:i+2] for i in range(len(run)-1))
        return tokens

    def schema(self):
        snapshot = collect_schema(self.database)
        atomic_json(self.directory / "schema.snapshot.json", snapshot)
        return snapshot

    def entries(self):
        entries = json.loads(SEEDS.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError("内置知识目录格式错误")
        for path in sorted(self.directory.glob("entry-*.json")):
            item = json.loads(path.read_text(encoding="utf-8"))
            entries.append(item)
        ids = set()
        for item in entries:
            if not isinstance(item, dict) or item.get("kind") not in {"metric", "example"} or not isinstance(item.get("id"), str) or item["id"] in ids:
                raise ValueError("知识目录条目类型或编号无效／重复")
            if item.get("status") not in {"draft", "approved", "builtin"}:
                raise ValueError("知识审核状态无效")
            if item["kind"] == "metric":
                if any(not isinstance(item.get(k), str) or not item[k].strip() for k in ("name", "definition", "table")) or not isinstance(item.get("aliases"), list) or not item["aliases"] or any(not isinstance(a, str) or not a for a in item["aliases"]):
                    raise ValueError("指标目录字段格式不正确")
                if not isinstance(item.get("required_filters"), dict) or not item["required_filters"] or any(not isinstance(k, str) or not isinstance(v, str) for k, v in item["required_filters"].items()):
                    raise ValueError("指标过滤规则格式不正确")
            elif any(not isinstance(item.get(k), str) or not item[k].strip() for k in ("question", "sql")):
                raise ValueError("示例目录字段格式不正确")
            if not isinstance(item.get("review", {}), dict):
                raise ValueError("审核记录格式不正确")
            ids.add(item["id"])
        return entries

    @staticmethod
    def content(item):
        return {k: v for k, v in item.items() if k not in {"status", "review"}}

    def usable(self, schema):
        accepted = []
        for item in self.entries():
            review = item.get("review", {})
            if item.get("status") not in {"approved", "builtin"}:
                continue
            if not review.get("reviewer") or review.get("content_hash") != digest(self.content(item)):
                continue
            if review.get("schema_hash") != schema["fingerprint"]:
                continue
            if item["kind"] == "example":
                try:
                    validate_sql(item["sql"])
                except ValueError:
                    continue
            accepted.append(item)
        metrics = [item for item in accepted if item["kind"] == "metric"]
        metric_hash = digest([self.content(item) for item in metrics])
        return [item for item in accepted if item["kind"] == "metric" or item["review"].get("metric_hash") == metric_hash]

    def examples(self):
        return {item["question"]: item["sql"] for item in self.usable(self.schema()) if item["kind"] == "example"}

    def add(self, kind, payload):
        if kind not in {"metric", "example"} or not isinstance(payload, dict):
            raise ValueError("知识类型或内容无效")
        required = ("question", "sql") if kind == "example" else ("name", "aliases", "definition", "table", "required_filters")
        if any(key not in payload for key in required):
            raise ValueError("缺少知识必填字段")
        if kind == "example":
            if any(not isinstance(payload[k], str) or not payload[k].strip() for k in required):
                raise ValueError("问题和 SQL 不能为空")
        else:
            if any(not isinstance(payload[k], str) or not payload[k].strip() for k in ("name", "definition", "table")):
                raise ValueError("指标名称、定义和表不能为空")
            if not isinstance(payload["aliases"], list) or not payload["aliases"] or any(not isinstance(a, str) or not a.strip() for a in payload["aliases"]):
                raise ValueError("指标别名必须为非空字符串列表")
            if not isinstance(payload["required_filters"], dict) or not payload["required_filters"] or any(not isinstance(k, str) or not isinstance(v, str) for k, v in payload["required_filters"].items()):
                raise ValueError("当前指标规则仅支持字段到字符串值的非空等值过滤映射")
        identifier = uuid.uuid4().hex
        item = {"id": identifier, "kind": kind, **{k: payload[k] for k in required}, "status": "draft"}
        atomic_json(self.directory / f"entry-{identifier}.json", item)
        return item

    def approve(self, identifier, reviewer, expected=None):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("知识编号无效")
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError("必须提供审核人")
        path = self.directory / f"entry-{identifier}.json"
        # Use the same catalog validation on manual edits as on retrieval.
        item = next((entry for entry in self.entries() if entry["id"] == identifier), None)
        if item is None:
            raise ValueError("知识条目不存在")
        schema = self.schema()
        columns = {c["name"] for t in schema["tables"] for c in t["columns"]}
        if item["kind"] == "metric":
            if item["table"] != "orders" or not set(item["required_filters"]).issubset(columns):
                raise ValueError("指标引用了未授权的表或不存在的字段")
        else:
            validate_sql(item["sql"])
            accepted = self.usable(schema)
            if any(other["kind"] == "example" and other["id"] != item["id"] and other["question"] == item["question"] and other["sql"] != item["sql"] for other in accepted):
                raise ValueError("同名问题已存在不同 SQL，请使用能区分口径的问题名称")
            metrics = [m for m in accepted if m["kind"] == "metric" and self.matches(item["question"], m)]
            error = self.validate_metrics(item["sql"], metrics)
            if error:
                raise ValueError(error)
            result = self.executor.execute(item["sql"])
            actual = {"columns": result["columns"], "rows": [list(row) for row in result["rows"]]}
            if result["truncated"] or expected is None or actual != expected:
                raise ValueError("实际结果与人工预期不一致、未提供预期或结果被截断，不能审核通过")
        item["status"] = "approved"
        item["review"] = {"reviewer": reviewer.strip(), "reviewed_at": datetime.now(timezone.utc).isoformat(), "content_hash": digest(self.content(item)), "schema_hash": schema["fingerprint"]}
        if item["kind"] == "example":
            item["review"]["expected_hash"] = digest(expected)
            item["review"]["metric_hash"] = digest([self.content(m) for m in self.usable(schema) if m["kind"] == "metric"])
        atomic_json(path, item)
        return item

    @staticmethod
    def matches(question, metric):
        return any(alias in question for alias in metric["aliases"])

    @staticmethod
    def validate_metrics(sql, metrics):
        if not metrics:
            return ""
        tree = validate_sql(sql)
        selects, tables = list(tree.find_all(exp.Select)), list(tree.find_all(exp.Table))
        if len(selects) != 1 or len(tables) != 1:
            return "指标口径当前仅支持单表 SELECT，复杂查询需人工确认"

        def conjuncts(node):
            if isinstance(node, exp.And):
                return conjuncts(node.this) + conjuncts(node.expression)
            if isinstance(node, exp.Paren):
                return conjuncts(node.this)
            return [node]

        where = selects[0].args.get("where")
        conditions = conjuncts(where.this) if where else []
        for metric in metrics:
            if tables[0].name != metric["table"]:
                return "指标引用表与审核口径不一致：" + metric["name"]
            for column, value in metric["required_filters"].items():
                if not any(isinstance(eq, exp.EQ) and isinstance(eq.this, exp.Column) and eq.this.name == column and isinstance(eq.expression, exp.Literal) and eq.expression.is_string and eq.expression.this == value for eq in conditions):
                    return f"{metric['name']}需在 WHERE 中限定 {column}='{value}'"
        return ""

    def retrieve(self, question, top_k=2):
        schema = self.schema()
        items = self.usable(schema)
        usable_ids = {item["id"] for item in items}
        for item in self.entries():
            if item["kind"] == "metric" and item["status"] in {"builtin", "approved"} and self.matches(question, item) and item["id"] not in usable_ids:
                raise ValueError("相关指标审核已失效，请检查内容或 Schema 变更并重新审核：" + item["name"])
        # All accepted metric rules remain mandatory, so a top-k limit cannot hide them.
        metrics = [item for item in items if item["kind"] == "metric"]
        matched = [m for m in metrics if self.matches(question, m)]
        terms = self.terms(question)
        ranked = sorted(((len(terms & self.terms(item["question"])), item) for item in items if item["kind"] == "example"), key=lambda pair: (-pair[0], pair[1]["id"]))
        examples = [item for score, item in ranked if score > 0][:top_k]
        selected = metrics + examples
        version = digest({"schema": schema, "items": items})
        context = "SQLite 授权结构（自动采集，不含业务数据行）：\n" + json.dumps(schema, ensure_ascii=False)
        context += "\n已接受业务口径（builtin 为项目合成演示基准，approved 为本地操作员确认）：\n" + json.dumps(metrics, ensure_ascii=False)
        context += "\n相关正确 SQL 示例（参考用途，不替代安全校验）：\n" + json.dumps(examples, ensure_ascii=False)
        context += "\n仅允许单条 SELECT，缺少必要条件请追问。演示数据为2026年1至6月合成数据，未指定时间默认全部数据。"
        return {"context": context, "version": version, "metrics": matched, "sources": [{"kind": "schema", "id": schema["fingerprint"]}] + [{"kind": item["kind"], "id": item["id"], "status": item["status"]} for item in selected]}
