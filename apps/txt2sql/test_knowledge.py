import json
import os
import sqlite3
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from server import Application, init_demo
from knowledge import collect_schema, atomic_json


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "demo.sqlite"
        init_demo(self.path)
        self.app = Application(self.path)
        self.catalog = self.app.catalog

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def draft(self, question="订单总数", sql="SELECT COUNT(*) AS 总数 FROM orders"):
        return self.catalog.add("example", {"question": question, "sql": sql})

    def test_schema_collects_live_columns_and_ignores_unauthorized_tables(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE secrets (password TEXT)")
            conn.execute("ALTER TABLE orders ADD COLUMN channel TEXT")
        conn.close()
        schema = collect_schema(self.path)
        self.assertEqual([t["name"] for t in schema["tables"]], ["orders"])
        self.assertIn("channel", [c["name"] for c in schema["tables"][0]["columns"]])
        self.assertNotIn("password", json.dumps(schema))
        with self.assertRaises(ValueError):
            collect_schema(self.path, ("orders; DELETE FROM orders",))

    def test_foreign_keys_are_collected_only_between_authorized_tables(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            conn.execute("CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id))")
        conn.close()
        result = collect_schema(self.path, ("parent", "child"))
        child = next(t for t in result["tables"] if t["name"] == "child")
        self.assertEqual(child["foreign_keys"], [{"table": "parent", "from": "parent_id", "to": "id"}])
        self.assertEqual(collect_schema(self.path, ("child",))["tables"][0]["foreign_keys"], [])

    def test_draft_excluded_then_reviewed_example_used_without_restart(self):
        item = self.draft()
        self.assertNotIn("订单总数", self.catalog.examples())
        approved = self.catalog.approve(item["id"], "测试审核员", {"columns": ["总数"], "rows": [[120]]})
        self.assertEqual(approved["status"], "approved")
        with patch.dict(os.environ, {}, clear=True):
            result = self.app.ask("订单总数")
        self.assertEqual(result["rows"], [(120,)])
        self.assertIn(item["id"], [source["id"] for source in result["sources"]])
        self.assertTrue(result["knowledge_version"])
        self.assertNotIn("已按已完成订单", result["explanation"])

    def test_approval_requires_reviewer_and_matching_expected_result(self):
        item = self.draft()
        for reviewer, expected in [("", {"columns": ["总数"], "rows": [[120]]}), ("审核员", None), ("审核员", {"columns": ["总数"], "rows": [[999]]})]:
            with self.subTest(reviewer=reviewer, expected=expected), self.assertRaises(ValueError):
                self.catalog.approve(item["id"], reviewer, expected)
        self.assertNotIn("订单总数", self.catalog.examples())

    def test_unsafe_example_cannot_be_approved(self):
        item = self.draft(sql="DELETE FROM orders")
        with self.assertRaises(ValueError):
            self.catalog.approve(item["id"], "审核员", {"columns": [], "rows": []})
        self.assertEqual(self.app.executor.execute("SELECT COUNT(*) FROM orders")["rows"], [(120,)])

    def test_edited_example_needs_new_review(self):
        item = self.draft()
        approved = self.catalog.approve(item["id"], "审核员", {"columns": ["总数"], "rows": [[120]]})
        approved["sql"] = "SELECT 999 AS 总数"
        atomic_json(self.catalog.directory / f"entry-{item['id']}.json", approved)
        self.assertNotIn("订单总数", self.catalog.examples())

    def test_conflicting_examples_cannot_silently_override_each_other(self):
        first = self.draft()
        self.catalog.approve(first["id"], "审核员", {"columns": ["总数"], "rows": [[120]]})
        second = self.draft(sql="SELECT COUNT(*) AS 总数 FROM orders WHERE status='已完成'")
        with self.assertRaisesRegex(ValueError, "同名问题"):
            self.catalog.approve(second["id"], "审核员", {"columns": ["总数"], "rows": [[110]]})

    def test_schema_drift_invalidates_review_and_sales_query_fails_closed(self):
        item = self.draft()
        self.catalog.approve(item["id"], "审核员", {"columns": ["总数"], "rows": [[120]]})
        with sqlite3.connect(self.path) as conn:
            conn.execute("ALTER TABLE orders ADD COLUMN notes TEXT")
        conn.close()
        self.assertNotIn("订单总数", self.catalog.examples())
        with self.assertRaisesRegex(ValueError, "审核已失效"):
            self.catalog.retrieve("各地区销售额")

    def test_metric_review_enforces_new_alias_and_invalidates_examples(self):
        draft = self.catalog.add("metric", {"name": "有效成交额", "aliases": ["有效成交额"], "definition": "仅已完成订单", "table": "orders", "required_filters": {"status": "已完成"}})
        self.assertFalse(any(m["name"] == "有效成交额" for m in self.catalog.retrieve("有效成交额")["metrics"]))
        self.catalog.approve(draft["id"], "业务负责人")
        metrics = self.catalog.retrieve("有效成交额")["metrics"]
        self.assertTrue(any(m["name"] == "有效成交额" for m in metrics))
        self.assertTrue(self.catalog.validate_metrics("SELECT SUM(amount) FROM orders", metrics))
        self.assertEqual(self.catalog.validate_metrics("SELECT SUM(amount) FROM orders WHERE status='已完成'", metrics), "")
        # Accepted examples are tied to the metric set present at review time.
        self.assertEqual(self.catalog.examples(), {})

    def test_example_metric_bypass_is_rejected_even_when_expected_matches(self):
        item = self.draft(question="全部销售额", sql="SELECT SUM(amount) AS 销售额 FROM orders WHERE status='已完成' OR 1=1")
        with self.assertRaisesRegex(ValueError, "WHERE"):
            self.catalog.approve(item["id"], "审核员", {"columns": ["销售额"], "rows": [[0]]})

    def test_retrieval_ranks_chinese_overlap_and_reports_sources(self):
        result = self.catalog.retrieve("各地区销售额")
        examples = [source for source in result["sources"] if source["kind"] == "example"]
        self.assertEqual(examples[0]["id"], "demo-example-1")
        self.assertIn("primary_key_position", result["context"])
        self.assertIn("demo-sales", [source["id"] for source in result["sources"]])

    def test_builtin_examples_match_independent_demo_database(self):
        for item in self.catalog.entries():
            if item["kind"] == "example" and item["status"] == "builtin":
                with self.subTest(question=item["question"]):
                    result = self.app.executor.execute(item["sql"])
                    self.assertEqual({"columns": result["columns"], "rows": [list(row) for row in result["rows"]]}, item["expected"])

    def test_operator_cli_roundtrip(self):
        root = Path(__file__).parent
        definition = self.path.parent / "candidate.json"
        expected = self.path.parent / "expected.json"
        atomic_json(definition, {"question": "所有订单条数", "sql": "SELECT COUNT(*) AS 数量 FROM orders"})
        atomic_json(expected, {"columns": ["数量"], "rows": [[120]]})
        prefix = [sys.executable, str(root / "manage_knowledge.py"), "--database", str(self.path), "--catalog", str(self.catalog.directory)]
        candidate = subprocess.run(prefix + ["add-example", "--file", str(definition)], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(candidate.returncode, 0, candidate.stderr)
        identifier = json.loads(candidate.stdout)["id"]
        approved = subprocess.run(prefix + ["approve", identifier, "--reviewer", "operator", "--expected", str(expected)], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(approved.returncode, 0, approved.stderr)
        self.assertIn("所有订单条数", self.catalog.examples())

    def test_malformed_catalog_becomes_a_failed_response(self):
        atomic_json(self.catalog.directory / ("entry-" + "a" * 32 + ".json"), {"id": "a" * 32, "kind": "metric", "status": "approved"})
        with patch.dict(os.environ, {}, clear=True):
            result = self.app.ask("各地区销售额")
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("rows", result)


if __name__ == "__main__":
    unittest.main()
