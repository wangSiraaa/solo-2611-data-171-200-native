"""Result flattening and CSV / JSON writers."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .planner import PlanResult


def flatten_rows(result: PlanResult) -> tuple[list[str], list[dict]]:
    columns = list(result.columns)
    rows: list[dict] = []
    for row in result.rows:
        flat: dict[str, Any] = {}
        for d, v in row["group"].items():
            flat[d] = v
        for m, v in row["metrics"].items():
            flat[m] = v
        for rname, info in result.ratio_fields.items():
            flat[info["numerator_column"]] = row["base"][info["numerator"]]
            flat[info["denominator_column"]] = row["base"][info["denominator"]]
        for fact, col in result.fact_row_columns.items():
            flat[col] = row["fact_rows"][fact]
        rows.append(flat)
    return columns, rows


def write_csv(path: str | Path, columns: list[str], rows: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: _csv_value(row.get(c)) for c in columns})


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def write_json(path: str | Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def as_json_payload(columns: list[str], rows: list[dict],
                    plan: dict | None = None) -> dict:
    payload: dict[str, Any] = {"columns": columns, "rows": rows}
    if plan is not None:
        payload["plan"] = plan
    return payload


def print_table(columns: list[str], rows: list[dict]) -> None:
    """Simple fixed-width table for terminal display."""
    def render(v: Any) -> str:
        if v is None:
            return "null"
        return str(v)

    width = {c: len(c) for c in columns}
    for row in rows:
        for c in columns:
            width[c] = max(width[c], len(render(row.get(c))))
    line = "  ".join(c.ljust(width[c]) for c in columns)
    print(line)
    print("  ".join("-" * width[c] for c in columns))
    for row in rows:
        print("  ".join(render(row.get(c)).ljust(width[c]) for c in columns))
