"""Protocol-level stdio tests; install requirements-mcp.txt to run."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Ensures local .deps is on sys.path before importing the optional SDK.
import server

try:
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False


@unittest.skipUnless(MCP_AVAILABLE, "Install apps/txt2sql/requirements-mcp.txt")
class MCPTests(unittest.TestCase):
    def test_stdio_discovery_queries_and_persistent_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            async def run():
                params = StdioServerParameters(
                    command=sys.executable,
                    args=[str(Path(__file__).with_name("mcp_server.py").resolve()), "--data-dir", directory],
                    env={"TXT2SQL_API_KEY": "", "PYTHONUTF8": "1"},
                )
                with open(Path(directory) / "stderr.log", "w", encoding="utf-8") as errors:
                    async with stdio_client(params, errlog=errors) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            tools = (await session.list_tools()).tools
                            self.assertEqual({t.name for t in tools}, {"get_data_catalog", "ask_data", "resume_question", "get_query_status"})
                            self.assertTrue(next(t for t in tools if t.name == "get_query_status").annotations.readOnlyHint)
                            self.assertFalse(next(t for t in tools if t.name == "ask_data").annotations.idempotentHint)
                            catalog = self.decode(await session.call_tool("get_data_catalog", {}))
                            self.assertEqual([t["name"] for t in catalog["schema"]["tables"]], ["orders"])
                            self.assertEqual(catalog["allowed_operations"], ["SELECT"])
                            result = self.decode(await session.call_tool("ask_data", {"question": "已完成订单数"}))
                            self.assertEqual(result["status"], "completed")
                            self.assertEqual(result["rows"], [[110]])
                            status = self.decode(await session.call_tool("get_query_status", {"thread_id": result["thread_id"]}))
                            self.assertEqual(status["rows"], [[110]])
                            self.assertEqual(status["trace"], result["trace"])
                            invalid = self.decode(await session.call_tool("get_query_status", {"thread_id": "0" * 32}))
                            self.assertEqual(invalid["status"], "failed")
                            self.assertNotIn("rows", invalid)
                            finished = self.decode(await session.call_tool("resume_question", {"thread_id": result["thread_id"], "answer": "继续"}))
                            self.assertEqual(finished["status"], "failed")
                            waiting = self.decode(await session.call_tool("ask_data", {"question": "收入是多少"}))
                            self.assertEqual(waiting["status"], "interrupted")
                            persisted = self.decode(await session.call_tool("get_query_status", {"thread_id": waiting["thread_id"]}))
                            self.assertEqual(persisted["clarification"], waiting["clarification"])
                    # A new MCP process reads the same local checkpointer.
                    async with stdio_client(params, errlog=errors) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            status = self.decode(await session.call_tool("get_query_status", {"thread_id": waiting["thread_id"]}))
                            self.assertEqual(status["status"], "interrupted")
                            resumed = self.decode(await session.call_tool("resume_question", {"thread_id": waiting["thread_id"], "answer": "各地区销售额"}))
                            self.assertEqual(resumed["status"], "completed")
                            self.assertEqual(len(resumed["rows"]), 4)
                            self.assertEqual(resumed["thread_id"], waiting["thread_id"])
            async def bounded():
                with anyio.fail_after(45):
                    await run()
            anyio.run(bounded)

    @staticmethod
    def decode(result):
        if result.isError:
            raise AssertionError(result.content)
        return result.structuredContent or json.loads(result.content[0].text)


if __name__ == "__main__":
    unittest.main()
