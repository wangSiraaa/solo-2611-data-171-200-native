"""Cross-check every deterministic frame against PostgreSQL over a matrix.

This is a development/validation harness, not part of the explanation path.  It
generates many window specifications over the fixture rows and compares the
engine's deterministic values with PostgreSQL's.  Nondeterministic declarations
are checked for consistency (the observed value must be within the declared
set/range).

Run with ``WINDOW_LAB_DSN=... python -m tests.matrix``.
"""

from __future__ import annotations

import itertools
import sys

import psycopg

from window_lab.db import connect, default_dsn, load_rows
from window_lab.engine import explain
from window_lab.sqlparse import parse_query

TABLE = "window_events"

ORDERS = [
    ("tick NULLS LAST", []),
    ("tick NULLS FIRST", []),
    ("tick DESC NULLS LAST", []),
    ("tick DESC NULLS FIRST", []),
    ("tick NULLS LAST, event_id", []),
    ("tick NULLS LAST, amount NULLS FIRST", []),
]

FRAMES = [
    "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
    "ROWS BETWEEN 1 PRECEDING AND CURRENT ROW",
    "ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING",
    "ROWS BETWEEN CURRENT ROW AND 2 FOLLOWING",
    "ROWS BETWEEN 2 FOLLOWING AND 3 FOLLOWING",
    "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING",
    "ROWS BETWEEN 1 FOLLOWING AND 1 FOLLOWING",
    "ROWS BETWEEN 2 PRECEDING AND 1 PRECEDING",
    "GROUPS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
    "GROUPS BETWEEN 1 PRECEDING AND CURRENT ROW",
    "GROUPS BETWEEN 1 PRECEDING AND 1 FOLLOWING",
    "GROUPS BETWEEN CURRENT ROW AND 2 FOLLOWING",
    "GROUPS BETWEEN 2 FOLLOWING AND 3 FOLLOWING",
    "GROUPS BETWEEN 2 PRECEDING AND 1 PRECEDING",
    "GROUPS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING",
    "RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
    "RANGE BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING",
    "RANGE BETWEEN CURRENT ROW AND CURRENT ROW",
    "RANGE BETWEEN 1 PRECEDING AND CURRENT ROW",
    "RANGE BETWEEN CURRENT ROW AND 1 FOLLOWING",
    "RANGE BETWEEN 1 PRECEDING AND 2 FOLLOWING",
    "RANGE BETWEEN 2 PRECEDING AND 1 PRECEDING",
    "RANGE BETWEEN 1 FOLLOWING AND 2 FOLLOWING",
    "RANGE BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING",
    "RANGE BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING",
]

FUNCS = [
    ("SUM(amount)", "total"),
    ("COUNT(*)", "n_star"),
    ("COUNT(amount)", "n_amount"),
    ("FIRST_VALUE(amount)", "first_amount"),
    ("LAST_VALUE(amount)", "last_amount"),
]


def build_sql(order: str, frame: str) -> str:
    projections = ", ".join(f"{expr} OVER w AS {alias}" for expr, alias in FUNCS)
    return (
        f"SELECT event_id, {projections} FROM {TABLE} "
        f"WINDOW w AS (PARTITION BY account ORDER BY {order} {frame}) "
        f"ORDER BY event_id"
    )


def main() -> int:
    dsn = default_dsn()
    connection = connect(dsn)
    try:
        rows = load_rows(connection, TABLE)
    finally:
        connection.close()
    schema = {
        "table": TABLE,
        "identity": "event_id",
        "types": {
            "event_id": "integer",
            "account": "text",
            "tick": "integer nullable",
            "amount": "integer nullable",
        },
    }

    connection = psycopg.connect(dsn, autocommit=True)
    total = 0
    failures = 0
    nondet_cells = 0
    try:
        for order, frame in itertools.product([o for o, _ in ORDERS], FRAMES):
            sql = build_sql(order, frame)
            spec = {"query_id": f"{order}|{frame}", "sql": sql}
            # RANGE offsets need exactly one ORDER BY column; both the parser
            # and PostgreSQL reject the multi-key combinations, so confirm that
            # instead of treating them as a failure.
            multi_key = "," in order
            offset_range = frame.startswith("RANGE") and bool(
                __import__("re").search(r"\d+\s+(PRECEDING|FOLLOWING)", frame)
            )
            try:
                query = parse_query(spec, schema)
            except Exception as exc:  # noqa: BLE001 - deliberate rejection check
                if multi_key and offset_range:
                    continue
                raise
            if multi_key and offset_range:
                raise AssertionError(
                    f"parser accepted an invalid RANGE offset spec: {order} {frame}"
                )
            evidence = explain(query, rows)
            with connection.cursor() as cursor:
                cursor.execute(sql)
                columns = [d.name for d in cursor.description]
                observed = {
                    record[0]: dict(zip(columns, record)) for record in cursor.fetchall()
                }
            for result in evidence["results"]:
                total += 1
                event_id = result["event_id"]
                name = result["expression"]
                value = result["value"]
                actual = observed[event_id][name]
                if value["deterministic"]:
                    if value.get("value") != actual:
                        failures += 1
                        print(
                            f"MISMATCH {order} | {frame} | event {event_id} {name}: "
                            f"engine={value.get('value')} pg={actual}"
                        )
                else:
                    nondet_cells += 1
                    if "possible_values" in value and value["possible_values"] is not None:
                        allowed = value["possible_values"]
                        if actual not in allowed:
                            failures += 1
                            print(
                                f"BAD-SET {order} | {frame} | event {event_id} {name}: "
                                f"pg={actual} not in {allowed}"
                            )
                    else:
                        low, high = value["possible_min"], value["possible_max"]
                        ok = (actual is None and value.get("null_possible")) or (
                            actual is not None and low <= actual <= high
                        )
                        if not ok:
                            failures += 1
                            print(
                                f"BAD-RANGE {order} | {frame} | event {event_id} "
                                f"{name}: pg={actual} not in [{low},{high}] "
                                f"null_possible={value.get('null_possible')}"
                            )
    finally:
        connection.close()

    print(
        f"checked {total} cells across {len(ORDERS) * len(FRAMES)} window specs; "
        f"{nondet_cells} declared nondeterministic; {failures} failures"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
