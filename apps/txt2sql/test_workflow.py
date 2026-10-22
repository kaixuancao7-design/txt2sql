import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from server import Application, init_demo
from workflow import validate_sql


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "demo.sqlite"
        init_demo(self.path)
        self.app = Application(self.path)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.app.close()
        self.tmp.cleanup()

    def test_interrupt_survives_restart(self):
        waiting = self.app.ask("查收入")
        self.assertEqual(waiting["status"], "interrupted")
        self.app.close()
        self.app = Application(self.path)
        result = self.app.ask("各地区销售额", waiting["thread_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["rows"]), 4)
        with self.assertRaises(ValueError):
            self.app.ask("继续", waiting["thread_id"])

    def test_execution_error_repaired(self):
        with patch.object(self.app.planner, "generate", return_value={"sql": "SELECT missing FROM orders"}):
            with patch.object(self.app.planner, "fix", return_value={"sql": "SELECT COUNT(*) FROM orders"}) as fix:
                result = self.app.ask("已完成订单数")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(result["trace"].count("sql_security_validate"), 2)
        self.assertIn("missing", fix.call_args.args[3])

    def test_repair_cannot_bypass_security(self):
        with patch.object(self.app.planner, "generate", return_value={"sql": "SELECT missing FROM orders"}):
            with patch.object(self.app.planner, "fix", return_value={"sql": "DELETE FROM orders"}):
                result = self.app.ask("已完成订单数")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["trace"].count("execute_sql"), 1)
        self.assertNotIn("rows", result)

    def test_retry_exhaustion(self):
        with patch.object(self.app.planner, "generate", return_value={"sql": "SELECT missing FROM orders"}):
            with patch.object(self.app.planner, "fix", return_value={"sql": "SELECT missing FROM orders"}) as fix:
                result = self.app.ask("已完成订单数")
        self.assertEqual(fix.call_count, 2)
        self.assertEqual(result["retry_count"], 2)
        self.assertTrue(result["error"])
        self.assertEqual(result["trace"][-1], "final_answer")

    def test_empty_result_is_success(self):
        with patch.object(self.app.planner, "generate", return_value={"sql": "SELECT * FROM orders WHERE id < 0"}):
            result = self.app.ask("已完成订单数")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["retry_count"], 0)
        self.assertTrue(result["warnings"])

    def test_metric_violation_and_or_bypass_trigger_repair(self):
        for sql in ["SELECT SUM(amount) FROM orders", "SELECT SUM(amount) FROM orders WHERE status='已完成' OR 1=1"]:
            with self.subTest(sql=sql):
                with patch.object(self.app.planner, "generate", return_value={"sql": sql}):
                    with patch.object(self.app.planner, "fix", return_value={"sql": "SELECT SUM(amount) FROM orders WHERE status='已完成'"}):
                        result = self.app.ask("各地区销售额")
                self.assertEqual(result["retry_count"], 1)
                self.assertEqual(result["status"], "completed")

    def test_audit_has_stages_and_terminal_event(self):
        result = self.app.ask("各地区销售额")
        with sqlite3.connect(self.path.parent / "audit.sqlite") as conn:
            records = conn.execute("SELECT node,status FROM events WHERE thread_id=?", (result["thread_id"],)).fetchall()
        conn.close()
        self.assertIn(("execute_sql", "started"), records)
        self.assertIn(("final_answer", "completed"), records)

    def test_ast_scope_and_attacks(self):
        validate_sql("WITH x AS (SELECT * FROM orders) SELECT COUNT(*) FROM x")
        for sql in ["SELECT * FROM sqlite_master", "SELECT 1; SELECT 2", "DROP TABLE orders", "SELECT * FROM main.orders", "SELECT load_extension('x')", "WITH x AS (SELECT * FROM sqlite_master) SELECT * FROM x"]:
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                validate_sql(sql)

    def test_status_read_does_not_execute_sql(self):
        result = self.app.ask("已完成订单数")
        with patch.object(self.app.executor, "execute", side_effect=AssertionError("status must not execute SQL")):
            status = self.app.workflow.get_status(result["thread_id"])
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["rows"], [[110]])

    def test_incomplete_checkpoint_cannot_be_resumed_as_human_answer(self):
        interrupted_process = SimpleNamespace(values={"status": "running", "trace": ["generate_sql"]}, next=("execute_sql",), tasks=())
        with patch.object(self.app.workflow.graph, "get_state", return_value=interrupted_process):
            status = self.app.workflow.get_status("a" * 32)
            self.assertEqual(status["status"], "incomplete")
            self.assertNotIn("rows", status)
            with self.assertRaisesRegex(ValueError, "不处于追问"):
                self.app.ask("继续", "a" * 32)


if __name__ == "__main__":
    unittest.main()
