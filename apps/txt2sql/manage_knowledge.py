"""Local operator CLI. Run with --help for review commands."""

import argparse
import json
import sys
from pathlib import Path

from server import DATA, SQLiteExecutor
from knowledge import KnowledgeCatalog


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="本地 Schema 与业务知识审核；不会调用模型或自动批准")
    parser.add_argument("--database", type=Path, default=DATA / "demo.sqlite")
    parser.add_argument("--catalog", type=Path, default=DATA / "knowledge")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("schema", help="采集授权表结构并保存快照")
    commands.add_parser("list", help="列出草稿、内置基准和人工审核条目")
    for kind in ("metric", "example"):
        add = commands.add_parser("add-" + kind, help="从 JSON 文件添加草稿")
        add.add_argument("--file", type=Path, required=True)
    approve = commands.add_parser("approve", help="审核条目；示例必须提供人工预期结果")
    approve.add_argument("id")
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--expected", type=Path, help="JSON 文件，包含 columns 和 rows，按顺序逐项比较")
    args = parser.parse_args()
    if not args.database.is_file():
        parser.exit(1, "数据库不存在，请先启动应用生成演示库，或提供 --database。\n")
    executor = SQLiteExecutor(args.database)
    try:
        catalog = KnowledgeCatalog(args.database, args.catalog, executor)
        if args.command == "schema":
            result = catalog.schema()
        elif args.command == "list":
            usable = {item["id"] for item in catalog.usable(catalog.schema())}
            result = [{"id": item["id"], "kind": item["kind"], "name": item.get("name", item.get("question")), "status": item["status"], "usable": item["id"] in usable} for item in catalog.entries()]
        elif args.command.startswith("add-"):
            result = catalog.add(args.command[4:], json.loads(args.file.read_text(encoding="utf-8-sig")))
        else:
            expected = json.loads(args.expected.read_text(encoding="utf-8-sig")) if args.expected else None
            result = catalog.approve(args.id, args.reviewer, expected)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
    finally:
        executor.close()


if __name__ == "__main__":
    main()
