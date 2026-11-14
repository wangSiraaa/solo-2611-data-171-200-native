"""SQLite database bootstrap from a DDL/data script and connection helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def build_database(sql_path: str | Path, db_path: str | Path) -> Path:
    """Create (or recreate) a SQLite database from a schema+data script."""
    sql_path = Path(sql_path)
    db_path = Path(db_path)
    if not sql_path.exists():
        raise FileNotFoundError(f"SQL bootstrap script not found: {sql_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    script = sql_path.read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(script)
        conn.commit()
    finally:
        conn.close()
    return db_path


def open_database(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path} (run `build` first)")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn
