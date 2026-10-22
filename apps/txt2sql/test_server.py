import importlib.util
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("txt2sql_server", Path(__file__).with_name("server.py"))
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "demo.sqlite"
        server.init_demo(self.path)
        self.app = server.Application(self.path)

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def test_sales_matches_business_definition(self):
        with patch.dict(os.environ, {}, clear=True):
            result = self.app.ask("各地区销售额")
        with closing(sqlite3.connect(self.path)) as conn:
            total = conn.execute("SELECT SUM(amount) FROM orders WHERE status='已完成'").fetchone()[0]
        self.assertEqual(sum(row[1] for row in result["rows"]), total)

    def test_demo_clarifies_unknown_questions(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIn("clarification", self.app.ask("收入是多少"))

    def test_blocks_unsafe_sql(self):
        for sql in ["DELETE FROM orders", "DROP TABLE orders", "PRAGMA table_info(orders)",
                    "SELECT * FROM sqlite_master", "ATTACH DATABASE ':memory:' AS other",
                    "SELECT load_extension('anything')", "SELECT * FROM orders; DELETE FROM orders",
                    "WITH gone AS (SELECT * FROM orders) DELETE FROM orders"]:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.Error):
                self.app.executor.execute(sql)
        self.assertEqual(self.app.executor.execute("SELECT COUNT(*) FROM orders")["rows"], [(120,)])

    def test_cte_and_limits(self):
        self.app.executor.limit = 5
        result = self.app.executor.execute("WITH x AS (SELECT * FROM orders) SELECT * FROM x")
        self.assertEqual(len(result["rows"]), 5)
        self.assertTrue(result["truncated"])

    def test_timeout_interrupts_expensive_query(self):
        executor = self.app.executor
        executor.timeout = 0.01
        with self.assertRaises(sqlite3.OperationalError):
            executor.execute("SELECT COUNT(*) FROM orders a CROSS JOIN orders b CROSS JOIN orders c CROSS JOIN orders d")

    def test_model_plan_is_still_subject_to_permissions(self):
        with patch.object(self.app.planner, "generate", return_value={"sql": "DELETE FROM orders"}):
            result = self.app.ask("已完成订单数")
        self.assertEqual(result["status"], "rejected")
        self.assertNotIn("execute_sql", result["trace"])

    def test_invalid_input(self):
        for question in [None, "", " " * 10, "x" * 2001, []]:
            with self.subTest(question=question), self.assertRaises(ValueError):
                self.app.ask(question)

    def test_model_interface_parses_plan_without_exposing_key(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": '{"sql":"SELECT COUNT(*) FROM orders"}'}}]
        }).encode()
        config = {"TXT2SQL_API_KEY": "test-key", "TXT2SQL_MODEL": "test-model",
                  "TXT2SQL_MODEL_URL": "https://model.example/chat/completions"}
        with patch.dict(os.environ, config, clear=True):
            with patch.object(server.urllib.request, "urlopen", return_value=response) as call:
                result = self.app.planner.plan("所有订单数量")
        self.assertEqual(result["sql"], "SELECT COUNT(*) FROM orders")
        request = call.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertNotIn("test-key", json.dumps(result))

    def test_malformed_model_output_is_reported(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"choices":[]}'
        with patch.dict(os.environ, {"TXT2SQL_API_KEY": "test", "TXT2SQL_MODEL": "test",
                                     "TXT2SQL_MODEL_URL": "https://model.example/chat"}):
            with patch.object(server.urllib.request, "urlopen", return_value=response):
                with self.assertRaisesRegex(ValueError, "模型请求失败"):
                    self.app.planner.plan("查询订单")

    def test_http_flow_and_token(self):
        http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.app))
        worker = threading.Thread(target=http.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{http.server_port}"
        try:
            with urllib.request.urlopen(base + "/api/context") as response:
                context = json.load(response)
            payload = json.dumps({"question": "已完成订单数"}).encode()
            request = urllib.request.Request(base + "/api/ask", data=payload)
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(request)
            self.assertEqual(denied.exception.code, 403)
            request.add_header("X-App-Token", context["token"])
            with patch.dict(os.environ, {}, clear=True):
                with urllib.request.urlopen(request) as response:
                    result = json.load(response)
            self.assertEqual(result["rows"], [[110]])
            with urllib.request.urlopen(base) as response:
                self.assertIn("取数台", response.read().decode())
        finally:
            http.shutdown()
            http.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
