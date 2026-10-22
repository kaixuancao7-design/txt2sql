"""Local stdio MCP adapter. Protocol output only on stdout."""

import argparse
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# Load the application's optional local dependency directory before MCP imports.
from server import Application, DATA, init_demo
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations


def create_server(data_dir: Path) -> FastMCP:
    data_dir = data_dir.resolve()

    @asynccontextmanager
    async def lifespan(server):
        database = data_dir / "demo.sqlite"
        init_demo(database)
        application = Application(database)
        server.application = application
        try:
            yield
        finally:
            application.close()

    mcp = FastMCP(
        "txt2sql",
        instructions="本地单用户只读问数。先查看 get_data_catalog，再 ask_data；interrupted 时向用户追问并用 resume_question 恢复。拒绝或失败后不要绕过权限。get_query_status 仅读取已有状态，不启动查询。",
        lifespan=lifespan,
        log_level="WARNING",
    )

    def application():
        app = getattr(mcp, "application", None)
        if app is None:
            raise ValueError("服务尚未启动")
        return app

    def safe_call(function, *args):
        try:
            return function(*args)
        except (ValueError, OSError, sqlite3.Error) as exc:
            return {"status": "failed", "error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def get_data_catalog() -> dict[str, Any]:
        """查看本地授权数据表和有效口径；只刷新本地结构快照，不查询业务数据行。"""
        def collect():
            catalog = application().catalog
            schema = catalog.schema()
            items = catalog.usable(schema)
            return {
                "status": "completed", "data_source": "demo_sales", "mode": "local_single_user",
                "schema": schema,
                "metrics": [{"name": item["name"], "aliases": item["aliases"], "definition": item["definition"], "status": item["status"]} for item in items if item["kind"] == "metric"],
                "example_questions": [item["question"] for item in items if item["kind"] == "example"],
                "allowed_operations": ["SELECT"], "row_limit": application().executor.limit,
                "scope": "合成 SQLite 数据；没有多人身份认证或远程共享服务。",
            }
        return safe_call(collect)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    def ask_data(question: str) -> dict[str, Any]:
        """提交新的自然语言问数，写入本地会话与审计；业务数据库只读。可能返回 interrupted、completed、failed 或 rejected。"""
        return safe_call(application().ask, question)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    def resume_question(thread_id: str, answer: str) -> dict[str, Any]:
        """只对 interrupted 会话提交用户实际补充的回答，恢复原问题。会话编号来自本服务。"""
        return safe_call(application().ask, answer, thread_id)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def get_query_status(thread_id: str) -> dict[str, Any]:
        """读取本地已有会话的追问、结果或失败状态；不会重跑 SQL，也不会自动继续未完成任务。"""
        return safe_call(application().workflow.get_status, thread_id)

    return mcp


def main():
    parser = argparse.ArgumentParser(description="TXT2SQL 本地单用户 MCP stdio 服务")
    parser.add_argument("--data-dir", type=Path, default=DATA / "mcp", help="本地操作员设置的独立状态目录，默认 data/mcp")
    args = parser.parse_args()
    create_server(args.data_dir).run(transport="stdio")


if __name__ == "__main__":
    main()
