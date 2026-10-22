"""Shared SQL safety rules for generation and reviewed examples."""

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

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
