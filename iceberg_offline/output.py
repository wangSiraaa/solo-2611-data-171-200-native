"""Writing independently readable results.

Outputs intentionally carry provenance columns (``_iceberg_file_path`` and
``_iceberg_physical_row``) so every retained row can be traced back to its
original data file and zero-based physical row.  They are arbitrary Parquet
files, not Iceberg tables.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .scan import RowDecision, SnapshotScan

PROVENANCE_FILE_PATH = "_iceberg_file_path"
PROVENANCE_PHYSICAL_ROW = "_iceberg_physical_row"
PROVENANCE_DATA_SEQUENCE = "_iceberg_data_sequence"
PROVENANCE_FILE_SEQUENCE = "_iceberg_file_sequence"
PROVENANCE_SNAPSHOT_ID = "_iceberg_snapshot_id"

_RESERVED = {
    PROVENANCE_FILE_PATH,
    PROVENANCE_PHYSICAL_ROW,
    PROVENANCE_DATA_SEQUENCE,
    PROVENANCE_FILE_SEQUENCE,
    PROVENANCE_SNAPSHOT_ID,
}


def _arrow_type(field) -> pa.DataType:
    if field.type == "long":
        return pa.int64()
    if field.type == "string":
        return pa.string()
    raise AssertionError(field.type)


def _ensure_not_conflicting(schema) -> None:
    conflict = set(schema.field_names) & _RESERVED
    if conflict:
        raise ValueError(
            f"schema column name(s) collide with provenance columns: {sorted(conflict)}"
        )


def _data_table(schema, rows: list[RowDecision]) -> pa.Table:
    _ensure_not_conflicting(schema)
    columns: dict[str, list[Any]] = {}
    for schema_field in schema.fields:
        columns[schema_field.name] = [r.values_by_name[schema_field.name] for r in rows]
    columns[PROVENANCE_FILE_PATH] = [r.file_state.file_path for r in rows]
    columns[PROVENANCE_PHYSICAL_ROW] = [r.physical_row for r in rows]
    columns[PROVENANCE_DATA_SEQUENCE] = [r.data_sequence for r in rows]
    columns[PROVENANCE_FILE_SEQUENCE] = [r.file_sequence for r in rows]
    columns[PROVENANCE_SNAPSHOT_ID] = [r.snapshot_id for r in rows]

    arrays = []
    names = []
    for schema_field in schema.fields:
        arrays.append(pa.array(columns[schema_field.name], type=_arrow_type(schema_field)))
        names.append(schema_field.name)
    arrays.extend(
        [
            pa.array(columns[PROVENANCE_FILE_PATH], type=pa.string()),
            pa.array(columns[PROVENANCE_PHYSICAL_ROW], type=pa.int64()),
            pa.array(columns[PROVENANCE_DATA_SEQUENCE], type=pa.int64()),
            pa.array(columns[PROVENANCE_FILE_SEQUENCE], type=pa.int64()),
            pa.array(columns[PROVENANCE_SNAPSHOT_ID], type=pa.int64()),
        ]
    )
    names.extend(
        [
            PROVENANCE_FILE_PATH,
            PROVENANCE_PHYSICAL_ROW,
            PROVENANCE_DATA_SEQUENCE,
            PROVENANCE_FILE_SEQUENCE,
            PROVENANCE_SNAPSHOT_ID,
        ]
    )
    return pa.Table.from_arrays(arrays, names=names)


def write_visible_rows(path: str | Path, schema, scan: SnapshotScan) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = _data_table(schema, scan.visible_rows)
    pq.write_table(table, str(path))
    return path


def write_file_visibility(path: str | Path, scan: SnapshotScan) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "snapshot_id",
                "manifest_path",
                "manifest_sequence_number",
                "manifest_entry_index",
                "status",
                "content",
                "file_path",
                "live",
                "data_sequence",
                "file_sequence",
                "data_sequence_inherited",
                "file_sequence_inherited",
                "record_count",
                "entry_snapshot_id",
            ]
        )
        for state in scan.file_states:
            writer.writerow(
                [
                    state.snapshot_id,
                    state.manifest_path,
                    state.manifest_sequence_number,
                    state.entry_index,
                    state.status_name,
                    state.content_name,
                    state.file_path,
                    "yes" if state.live else "no",
                    state.data_sequence,
                    state.file_sequence,
                    "yes" if state.inherited_sequence else "no",
                    "yes" if state.inherited_file_sequence else "no",
                    state.record_count,
                    "" if state.entry_snapshot_id is None else state.entry_snapshot_id,
                ]
            )
    return path


def write_row_evidence(path: str | Path, scan: SnapshotScan) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in scan.rows:
            fh.write(json.dumps(row.as_evidence_dict(), ensure_ascii=False, sort_keys=True))
            fh.write("\n")
    return path


def write_manifest_plan(path: str | Path, scan: SnapshotScan) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "snapshot_id": scan.snapshot_id,
        "snapshot_sequence_number": scan.sequence_number,
        "manifest_list": scan.manifest_list_path,
        "manifests": list(scan.manifest_paths),
        "data_files_read": list(scan.data_files_read),
        "delete_files_read": list(scan.delete_files_read),
        "visible_row_count": len(scan.visible_rows),
        "deleted_row_count": len(scan.deleted_rows),
        "scanned_row_count": len(scan.rows),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def write_snapshot_outputs(out_dir: str | Path, schema, scan: SnapshotScan) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    return {
        "rows": write_visible_rows(out / "rows.parquet", schema, scan),
        "files": write_file_visibility(out / "files.csv", scan),
        "evidence": write_row_evidence(out / "rows.jsonl", scan),
        "plan": write_manifest_plan(out / "plan.json", scan),
    }
