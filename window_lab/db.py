"""PostgreSQL access limited to a dedicated test database.

The tool never connects to a production database.  Every command requires an
explicit connection target (DSN or host/port/database), refuses the
``postgres``/``template*`` maintenance databases, and only ever touches the
single table named by the schema file.  There is no code path that creates or
mutates anything other than the in-scope fixture table loaded from
``fixtures/seed.sql``.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

from .engine import Row

_SEED_RELATIVE = Path("fixtures") / "seed.sql"
_FORBIDDEN_DATABASES = {"postgres", "template0", "template1"}


class DatabaseError(Exception):
    pass


def default_dsn() -> str:
    """Connection string from ``WINDOW_LAB_DSN``.

    Example: ``host=/tmp port=5433 dbname=window_lab``.
    """
    dsn = os.environ.get("WINDOW_LAB_DSN", "").strip()
    if not dsn:
        raise DatabaseError(
            "no database configured; set WINDOW_LAB_DSN, e.g. "
            "WINDOW_LAB_DSN='host=/tmp port=5433 dbname=window_lab'"
        )
    return dsn


def _check_target(dsn: str) -> None:
    info = psycopg.conninfo.conninfo_to_dict(dsn)
    dbname = info.get("dbname") or info.get("database")
    if not dbname:
        raise DatabaseError("connection string must name an explicit test database")
    if dbname in _FORBIDDEN_DATABASES:
        raise DatabaseError(
            f"refusing to use maintenance database {dbname!r}; use a dedicated "
            "test database"
        )


def connect(dsn: str | None = None) -> psycopg.Connection:
    dsn = dsn or default_dsn()
    _check_target(dsn)
    try:
        return psycopg.connect(dsn, autocommit=True)
    except psycopg.Error as exc:
        raise DatabaseError(f"cannot connect: {exc}") from exc


def seed_path() -> Path:
    candidate = Path(__file__).resolve().parent.parent / _SEED_RELATIVE
    if not candidate.exists():
        raise DatabaseError(f"seed file not found: {candidate}")
    return candidate


def ensure_fixture_table(connection: psycopg.Connection, table: str) -> None:
    """Load ``fixtures/seed.sql`` iff it builds the requested table and it is absent.

    Only the fixture table is ever created.  If ``table`` is a different
    single-table supplement, the caller must initialize it explicitly; the
    tool will not silently create unrelated tables.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s)", (table,))
        if cursor.fetchone()[0] is not None:
            return
    seed = seed_path().read_text(encoding="utf-8")
    if f"TABLE {table}(" not in seed.replace('"', "").replace(" ", ""):
        raise DatabaseError(
            f"table {table!r} does not exist; load its fixture SQL before running "
            "the tool (use --init-sql for the manual supplement)"
        )
    execute_sql(connection, seed)


def execute_sql(connection: psycopg.Connection, sql: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(sql)


def load_rows(connection: psycopg.Connection, table: str) -> list[Row]:
    """Read every row of the single in-scope table in a stable order."""
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT * FROM "{table}" ORDER BY 1')
        columns = [description.name for description in cursor.description]
        rows: list[Row] = []
        for index, record in enumerate(cursor.fetchall()):
            values = dict(zip(columns, record))
            rows.append(
                Row(event_id=values.get("event_id"), values=values, input_index=index)
            )
    return rows


def run_query(connection: psycopg.Connection, sql: str) -> dict[int, dict]:
    """Execute the original SQL and return ``event_id -> row values``.

    Cross-check material only: the explanation is derived before this runs.
    """
    with connection.cursor() as cursor:
        cursor.execute(sql)
        columns = [description.name for description in cursor.description]
        results: dict[int, dict] = {}
        for record in cursor.fetchall():
            row = dict(zip(columns, record))
            event_id = row.get("event_id")
            if event_id is None:
                raise DatabaseError("query result is missing the identity column")
            results[event_id] = row
    return results
