"""Verification: compare independent explanation with PostgreSQL output.

The database is used only to cross-check results that the explanation declares
deterministic.  Nondeterministic rows are never asserted equal to a single
database value; instead the tool checks that the observed value is one of the
declared possibilities (and, for SUM/COUNT, within the declared range).
"""

from __future__ import annotations

from typing import Optional

from .db import connect, ensure_fixture_table, load_rows, run_query
from .engine import Row, explain
from .sqlparse import Query, parse_query


def _values_match(expected, actual, tolerance: float = 1e-9) -> bool:
    if expected is None or actual is None:
        return expected is None and actual is None
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(expected - actual) <= tolerance
    return expected == actual


def verify(
    query: Query,
    rows: list[Row],
    dsn: Optional[str] = None,
) -> dict:
    """Derive evidence, then call PostgreSQL to cross-check.

    Returns a report with per-cell statuses:
    ``match`` / ``mismatch`` / ``nondeterministic`` / ``not_checked``.
    """
    evidence = explain(query, rows)

    connection = connect(dsn)
    try:
        actual_rows = run_query(connection, query.sql)
        # Confirm the data we explained matches what PostgreSQL sees.
        db_rows = load_rows(connection, query.table)
    finally:
        connection.close()

    expected_ids = sorted(row.event_id for row in rows)
    db_ids = sorted(row.event_id for row in db_rows)
    data_consistent = expected_ids == db_ids

    checks: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for result in evidence["results"]:
        event_id = result["event_id"]
        name = result["expression"]
        seen.add((event_id, name))
        observed_row = actual_rows.get(event_id)
        if observed_row is None:
            checks.append(
                {
                    "event_id": event_id,
                    "expression": name,
                    "status": "not_checked",
                    "detail": "result row missing from database output",
                }
            )
            continue
        observed = observed_row.get(name)
        expected = result["value"]
        if expected["deterministic"]:
            if _values_match(expected.get("value"), observed):
                checks.append(
                    {
                        "event_id": event_id,
                        "expression": name,
                        "status": "match",
                        "explained": expected.get("value"),
                        "observed": observed,
                    }
                )
            else:
                checks.append(
                    {
                        "event_id": event_id,
                        "expression": name,
                        "status": "mismatch",
                        "explained": expected.get("value"),
                        "observed": observed,
                        "detail": "derived deterministic value disagrees with PostgreSQL",
                    }
                )
            continue
        checks.append(_check_nondeterministic(event_id, name, expected, observed))

    missing = set(actual_rows) - {row.event_id for row in rows}
    if missing:
        checks.append(
            {
                "event_id": None,
                "expression": None,
                "status": "not_checked",
                "detail": f"database returned unexpected event_ids: {sorted(missing)}",
            }
        )

    mismatches = [check for check in checks if check["status"] == "mismatch"]
    return {
        "query_id": query.query_id,
        "data_consistent": data_consistent,
        "row_count": len(rows),
        "checks": checks,
        "summary": {
            "match": sum(1 for check in checks if check["status"] == "match"),
            "mismatch": len(mismatches),
            "nondeterministic": sum(
                1 for check in checks if check["status"] == "nondeterministic"
            ),
            "not_checked": sum(1 for check in checks if check["status"] == "not_checked"),
        },
        "verified": data_consistent and not mismatches,
    }


def _check_nondeterministic(event_id: int, name: str, expected: dict, observed) -> dict:
    """Check an observed value is consistent with a nondeterministic declaration."""
    possible_values = expected.get("possible_values")
    low = expected.get("possible_min")
    high = expected.get("possible_max")
    null_possible = bool(expected.get("null_possible"))
    ok = False
    detail = ""
    if possible_values is not None:
        ok = any(_values_match(candidate, observed) for candidate in possible_values)
        detail = (
            "observed value is one of the declared possibilities"
            if ok
            else f"observed value {observed!r} is not among {possible_values!r}"
        )
    elif low is not None and high is not None:
        if observed is None:
            ok = null_possible
            detail = (
                "observed NULL is allowed by the declared possibilities"
                if ok
                else "observed NULL but NULL was declared impossible"
            )
        elif not isinstance(observed, (int, float)):
            ok = False
            detail = f"observed non-numeric value {observed!r} for SUM/COUNT"
        else:
            ok = low <= observed <= high
            detail = (
                f"observed {observed} lies within declared range [{low}, {high}]"
                if ok
                else f"observed {observed} outside declared range [{low}, {high}]"
            )
    else:
        detail = "no declared possibilities recorded"
    return {
        "event_id": event_id,
        "expression": name,
        "status": "nondeterministic" if ok else "mismatch",
        "explained": {
            "possible_min": low,
            "possible_max": high,
            "possible_values": possible_values,
            "null_possible": null_possible,
        },
        "observed": observed,
        "detail": detail,
    }


def verify_sql(query_spec: dict, schema: dict, dsn: Optional[str] = None) -> dict:
    """Parse, derive, and verify one query directly from its JSON spec."""
    query = parse_query(query_spec, schema)
    connection = connect(dsn)
    try:
        ensure_fixture_table(connection, query.table)
        rows = load_rows(connection, query.table)
    finally:
        connection.close()
    return verify(query, rows, dsn)
