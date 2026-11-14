#!/usr/bin/env python3
"""命令行入口：compile 与 verify 两个子命令。

用法：
  .venv/bin/python run.py compile   --out deliverables/compiled.json
  .venv/bin/python run.py verify    --out deliverables/verified.json \\
        --pg-socket /tmp/task179-sock --db taskdb --sqlite .runtime/target.db
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from pg2sqlite import compile_report
from pg2sqlite.verifier import build_sqlite_db, verify_report

ROOT = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(ROOT, "fixtures")


def _load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


def _load_json(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


def cmd_compile(args) -> int:
    queries = _load_json("queries.json")
    pg_ddl = _load("source.sql")
    lite_ddl = _load("target.sql")
    results = []
    for report in queries:
        outcome = compile_report(report, pg_ddl, lite_ddl)
        results.append(outcome)
        _print_outcome(outcome)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n已写出 {args.out}")
    return 0


def cmd_verify(args) -> int:
    import psycopg
    queries = _load_json("queries.json")
    pg_ddl = _load("source.sql")
    lite_ddl = _load("target.sql")
    rows_payload = _load_json("rows.json")
    rows = [dict(zip(rows_payload["events"]["columns"], r))
            for r in rows_payload["events"]["rows"]]

    os.makedirs(os.path.dirname(args.sqlite) or ".", exist_ok=True)
    if os.path.exists(args.sqlite):
        os.remove(args.sqlite)
    build_sqlite_db(args.sqlite, lite_ddl, rows_payload)

    conn_info = f"host={args.pg_socket} dbname={args.db} user={os.environ.get('USER','node')}"
    results = []
    with psycopg.connect(conn_info, autocommit=False) as pg_conn:
        for report in queries:
            qid = report["query_id"]
            outcome = compile_report(report, pg_ddl, lite_ddl)
            if outcome["status"] == "rejected":
                _print_outcome(outcome)
                results.append({"query_id": qid,
                                "status": "rejected",
                                "error": outcome["error"],
                                "executed": False})
                continue
            from pg2sqlite.compiler import compile_query
            compiled = compile_query(
                report["sql"], report["parameter_types"],
                pg_ddl, lite_ddl, query_id=qid)
            try:
                vr = verify_report(report, compiled, pg_conn,
                                   args.sqlite, rows)
                payload = vr.as_dict()
                payload["executed"] = True
                payload["compiled"] = outcome["compiled"]
                results.append(payload)
                print(f"[verified] {qid}: 两端各 {vr.pg_row_count} 行，逐行一致")
                for c in vr.checks:
                    print("   -", c)
            except AssertionError as exc:
                results.append({"query_id": qid, "status": "verify_failed",
                                "detail": str(exc)})
                print(f"[verify_failed] {qid}: {exc}")
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n已写出 {args.out}")
    return 0


def _print_outcome(outcome: dict) -> None:
    if outcome["status"] == "compiled":
        c = outcome["compiled"]
        print(f"[compiled] {outcome['query_id']}")
        print("   SQLite SQL:", c["sqlite_sql"])
        print("   绑定映射:", c["binding_map"])
        print("   输出类型:", [(s["name"], s["logical_type"])
                              for s in c["output_schema"]])
        for r in c["rewrites"]:
            print("   改写:", r)
        for e in c["distinct_evidence"]:
            print("   DISTINCT 依据:", e)
    else:
        e = outcome["error"]
        print(f"[rejected] {outcome['query_id']}: rule={e['rule']} "
              f"{e['message']}")
        if "position" in e:
            print("   原位置:", e["position"])
            print(e["snippet"])


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("compile")
    p1.add_argument("--out", default="deliverables/compiled.json")
    p1.set_defaults(func=cmd_compile)
    p2 = sub.add_parser("verify")
    p2.add_argument("--out", default="deliverables/verified.json")
    p2.add_argument("--pg-socket", default="/tmp/task179-sock")
    p2.add_argument("--db", default="taskdb")
    p2.add_argument("--sqlite", default=".runtime/target.db")
    p2.set_defaults(func=cmd_verify)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
