"""Command-line interface for the window-frame explanation tool.

Usage::

    window-lab explain --query-id rows --out evidence/rows.json
    window-lab verify  --query-id rows --out evidence/rows.verify.json
    window-lab all     --out evidence/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .db import DatabaseError, connect, ensure_fixture_table, load_rows
from .engine import explain
from .sqlparse import ParseError, parse_query
from .verify import verify

_DEFAULT_SCHEMA = Path("fixtures/schema.json")
_DEFAULT_QUERIES = Path("fixtures/queries.json")


def _load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _find_query(queries: list[dict], query_id: str) -> dict:
    for spec in queries:
        if spec.get("query_id") == query_id:
            return spec
    raise SystemExit(f"unknown query_id {query_id!r}")


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _load_rows(dsn: str, table: str) -> list:
    connection = connect(dsn)
    try:
        ensure_fixture_table(connection, table)
        return load_rows(connection, table)
    finally:
        connection.close()


def _apply_init_sql(dsn: str, init_sql: list[str]) -> None:
    from .db import execute_sql

    if not init_sql:
        return
    connection = connect(dsn)
    try:
        for path in init_sql:
            execute_sql(connection, Path(path).read_text(encoding="utf-8"))
    finally:
        connection.close()


def command_explain(args) -> int:
    schema = _load_json(Path(args.schema))
    queries = _load_json(Path(args.queries))
    spec = _find_query(queries, args.query_id)
    query = parse_query(spec, schema)
    _apply_init_sql(args.dsn, args.init_sql)
    rows = _load_rows(args.dsn, query.table)
    evidence = explain(query, rows)
    _write(Path(args.out), evidence)
    nondet = evidence["summary"]["nondeterministic_values"]
    print(
        f"{args.query_id}: {len(evidence['results'])} result cells, "
        f"{nondet} nondeterministic -> {args.out}"
    )
    return 0


def command_verify(args) -> int:
    schema = _load_json(Path(args.schema))
    queries = _load_json(Path(args.queries))
    spec = _find_query(queries, args.query_id)
    query = parse_query(spec, schema)
    _apply_init_sql(args.dsn, args.init_sql)
    rows = _load_rows(args.dsn, query.table)
    report = verify(query, rows, args.dsn)
    _write(Path(args.out), report)
    summary = report["summary"]
    print(
        f"{args.query_id}: match={summary['match']} mismatch={summary['mismatch']} "
        f"nondeterministic={summary['nondeterministic']} "
        f"not_checked={summary['not_checked']} verified={report['verified']}"
    )
    return 0 if report["verified"] else 1


def command_all(args) -> int:
    schema = _load_json(Path(args.schema))
    queries = _load_json(Path(args.queries))
    out_dir = Path(args.out)
    _apply_init_sql(args.dsn, args.init_sql)
    overall = 0
    for spec in queries:
        query = parse_query(spec, schema)
        rows = _load_rows(args.dsn, query.table)
        evidence = explain(query, rows)
        _write(out_dir / f"{spec['query_id']}.json", evidence)
        report = verify(query, rows, args.dsn)
        _write(out_dir / f"{spec['query_id']}.verify.json", report)
        summary = report["summary"]
        print(
            f"{spec['query_id']}: match={summary['match']} "
            f"mismatch={summary['mismatch']} "
            f"nondeterministic={summary['nondeterministic']} "
            f"verified={report['verified']}"
        )
        if summary["mismatch"]:
            overall = 1
    return overall


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="window-lab", description=__doc__)
    parser.add_argument(
        "--dsn",
        default=None,
        help="PostgreSQL connection string (defaults to $WINDOW_LAB_DSN)",
    )
    parser.add_argument("--schema", default=str(_DEFAULT_SCHEMA))
    parser.add_argument("--queries", default=str(_DEFAULT_QUERIES))
    parser.add_argument(
        "--init-sql",
        action="append",
        default=[],
        metavar="PATH",
        help="SQL file executed before reading rows (repeatable); used for the "
        "manual frame_modes supplement",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    explain_parser = subparsers.add_parser("explain", help="write structured evidence")
    explain_parser.add_argument("--query-id", required=True)
    explain_parser.add_argument("--out", required=True)
    explain_parser.set_defaults(func=command_explain)

    verify_parser = subparsers.add_parser("verify", help="cross-check against PostgreSQL")
    verify_parser.add_argument("--query-id", required=True)
    verify_parser.add_argument("--out", required=True)
    verify_parser.set_defaults(func=command_verify)

    all_parser = subparsers.add_parser("all", help="explain and verify every fixture query")
    all_parser.add_argument("--out", default="evidence")
    all_parser.set_defaults(func=command_all)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ParseError as exc:
        print(f"parse error: {exc}", file=sys.stderr)
        return 2
    except DatabaseError as exc:
        print(f"database error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
