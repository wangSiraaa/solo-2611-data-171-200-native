"""Snapshot planning and row-level delete resolution.

This is the core read path.  It deliberately keeps two separate numbers for
every file:

* ``data_sequence`` -- from the manifest entry's ``sequence_number`` (or the
  manifest-list entry when null).  Controls delete eligibility.
* ``file_sequence`` -- from ``file_sequence_number`` (or inherited).  Records
  when the file was added; it never influences deletion eligibility.

The current snapshot id is never used to overwrite either number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import InvalidTableError, RowNotFoundError, UnsupportedError
from .manifest import (
    CONTENT_DATA,
    CONTENT_EQUALITY_DELETE,
    CONTENT_NAMES,
    CONTENT_POSITION_DELETE,
    Manifest,
    ManifestEntry,
    iter_snapshot_manifests,
)
from .metadata import TableMetadata
from .parquet_io import (
    ParquetInfo,
    iter_rows,
    open_parquet,
    reject_unknown_fields,
    require_field,
)
from .plan import Plan

POSITION_DELETE_FILE_PATH_ID = 2147483546
POSITION_DELETE_POS_ID = 2147483545


@dataclass(frozen=True)
class FileState:
    """One manifest entry as seen through one snapshot."""

    snapshot_id: int
    manifest_path: str
    manifest_sequence_number: int
    status: int
    status_name: str
    content: int
    content_name: str
    file_path: str
    data_sequence: int
    file_sequence: int
    inherited_sequence: bool
    inherited_file_sequence: bool
    record_count: int
    entry_snapshot_id: int | None
    entry_index: int

    @property
    def live(self) -> bool:
        return self.status in (0, 1)

    @property
    def role(self) -> str:
        if self.content == CONTENT_DATA:
            return "data"
        if self.content == CONTENT_POSITION_DELETE:
            return "position_delete"
        if self.content == CONTENT_EQUALITY_DELETE:
            return "equality_delete"
        return "unknown"

    @property
    def visible_as_live_data(self) -> bool:
        return self.live and self.content == CONTENT_DATA


@dataclass(frozen=True)
class DeleteRecord:
    delete_file_path: str
    content: int
    delete_sequence: int
    physical_row: int
    reason: str
    key_values: tuple[Any, ...] | None = None
    key_names: tuple[str, ...] | None = None
    target_file_path: str | None = None

    @property
    def content_name(self) -> str:
        return CONTENT_NAMES.get(self.content, f"unknown({self.content})")

    def as_dict(self) -> dict[str, Any]:
        return {
            "delete_file_path": self.delete_file_path,
            "content": self.content_name,
            "delete_sequence": self.delete_sequence,
            "physical_row": self.physical_row,
            "reason": self.reason,
            "key_fields": list(self.key_names) if self.key_names else None,
            "key_values": list(self.key_values) if self.key_values is not None else None,
            "target_file_path": self.target_file_path,
        }


@dataclass
class RowDecision:
    snapshot_id: int
    file_state: FileState
    physical_row: int
    row_group: int
    row_in_group: int
    values_by_id: dict[int, Any]
    values_by_name: dict[str, Any]
    data_sequence: int
    file_sequence: int
    deleted: bool
    evidence: list[DeleteRecord] = field(default_factory=list)

    def as_evidence_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "file_path": self.file_state.file_path,
            "physical_row": self.physical_row,
            "row_group": self.row_group,
            "row_in_group": self.row_in_group,
            "data_sequence": self.data_sequence,
            "file_sequence": self.file_sequence,
            "deleted": self.deleted,
            "values": self.values_by_name,
            "evidence": [e.as_dict() for e in self.evidence],
        }


@dataclass
class SnapshotScan:
    snapshot_id: int
    sequence_number: int
    manifest_list_path: str
    manifest_paths: tuple[str, ...]
    file_states: tuple[FileState, ...]
    rows: list[RowDecision]
    data_files_read: tuple[str, ...]
    delete_files_read: tuple[str, ...]

    @property
    def visible_rows(self) -> list[RowDecision]:
        return [r for r in self.rows if not r.deleted]

    @property
    def deleted_rows(self) -> list[RowDecision]:
        return [r for r in self.rows if r.deleted]


@dataclass
class _PositionDeleteIndex:
    # exact full file path -> list of (position, DeleteRecord)
    by_path: dict[str, list[tuple[int, DeleteRecord]]]


@dataclass
class _EqualityDeleteIndex:
    delete_sequence: int
    field_ids: tuple[int, ...]
    key_names: tuple[str, ...]
    keys: dict[tuple[Any, ...], list[DeleteRecord]]


@dataclass
class SnapshotPlan:
    scan: SnapshotScan
    schema: Any
    delete_indices: dict[str, Any]


def _file_state(
    entry: ManifestEntry, snapshot_id: int, manifest: Manifest, entry_index: int
) -> FileState:
    return FileState(
        snapshot_id=snapshot_id,
        manifest_path=manifest.list_entry.manifest_path,
        manifest_sequence_number=manifest.list_entry.sequence_number,
        status=entry.status,
        status_name=entry.status_name,
        content=entry.data_file.content,
        content_name=entry.data_file.content_name,
        file_path=entry.data_file.file_path,
        data_sequence=entry.sequence_number,
        file_sequence=entry.file_sequence_number,
        inherited_sequence=entry.inherited_sequence_number,
        inherited_file_sequence=entry.inherited_file_sequence_number,
        record_count=entry.data_file.record_count,
        entry_snapshot_id=entry.snapshot_id,
        entry_index=entry_index,
    )


def collect_manifest_entries(
    plan: Plan, metadata: TableMetadata, snapshot_id: int
) -> tuple[list[tuple[ManifestEntry, FileState, Manifest]], str]:
    """Return every entry of every manifest in the snapshot's manifest list."""
    snapshot = metadata.snapshot(snapshot_id)
    manifests = list(iter_snapshot_manifests(plan, snapshot, metadata))
    result: list[tuple[ManifestEntry, FileState, Manifest]] = []
    for manifest in manifests:
        for entry_index, entry in enumerate(manifest.entries):
            result.append(
                (
                    entry,
                    _file_state(entry, snapshot_id, manifest, entry_index),
                    manifest,
                )
            )
    return result, snapshot.manifest_list


def _check_data_file_schema(info: ParquetInfo, schema, file_path: str) -> None:
    reject_unknown_fields(info, set(schema.field_ids), what="data file")
    for schema_field in schema.fields:
        require_field(info, schema_field.id, schema_field.type, what="data file")


def _read_position_deletes(
    plan: Plan, state: FileState, reader_factory=None
) -> list[tuple[int, DeleteRecord, str]]:
    """Return (position, record, target_path) for one position delete file."""
    path = plan.resolve(state.file_path)
    info = open_parquet(path, reader_factory=reader_factory)
    try:
        reject_unknown_fields(
            info,
            {POSITION_DELETE_FILE_PATH_ID, POSITION_DELETE_POS_ID},
            what="position delete file",
        )
        require_field(
            info, POSITION_DELETE_FILE_PATH_ID, "string", what="position delete file"
        )
        require_field(info, POSITION_DELETE_POS_ID, "long", what="position delete file")
        rows: list[tuple[int, DeleteRecord, str]] = []
        for physical_row, _row_group, _row_in_group, values in iter_rows(
            info,
            [POSITION_DELETE_FILE_PATH_ID, POSITION_DELETE_POS_ID],
            what="position delete file",
        ):
            target = values[POSITION_DELETE_FILE_PATH_ID]
            pos = values[POSITION_DELETE_POS_ID]
            if not isinstance(target, str):
                raise InvalidTableError(
                    f"position delete {state.file_path} row {physical_row} has non-string file_path"
                )
            if not isinstance(pos, int) or isinstance(pos, bool) or pos < 0:
                raise InvalidTableError(
                    f"position delete {state.file_path} row {physical_row} has invalid pos {pos!r}"
                )
            record = DeleteRecord(
                delete_file_path=state.file_path,
                content=CONTENT_POSITION_DELETE,
                delete_sequence=state.data_sequence,
                physical_row=physical_row,
                reason="position",
                target_file_path=target,
            )
            rows.append((pos, record, target))
        if len(rows) != state.record_count:
            raise InvalidTableError(
                f"position delete file {state.file_path} has {len(rows)} rows, "
                f"manifest says {state.record_count}"
            )
        return rows
    finally:
        info.close()


def _read_equality_deletes(
    plan: Plan, state: FileState, schema, entry: ManifestEntry, reader_factory=None
) -> _EqualityDeleteIndex:
    equality_ids = entry.data_file.equality_ids
    if not equality_ids:
        raise InvalidTableError(
            f"equality delete file {state.file_path} has no equality_ids"
        )
    # Resolve the IDs against the table schema first; an equality id that is
    # not in the schema is an explicit unsupported structure.
    key_names: list[str] = []
    for field_id in equality_ids:
        if field_id not in schema.field_ids:
            raise UnsupportedError(
                f"unsupported: equality delete {state.file_path} uses field id "
                f"{field_id} not present in schema-id 0"
            )
        key_names.append(schema.by_id(field_id).name)

    path = plan.resolve(state.file_path)
    info = open_parquet(path, reader_factory=reader_factory)
    try:
        reject_unknown_fields(info, set(equality_ids), what="equality delete file")
        for field_id in equality_ids:
            schema_field = schema.by_id(field_id)
            require_field(info, field_id, schema_field.type, what="equality delete file")
        keys: dict[tuple[Any, ...], list[DeleteRecord]] = {}
        count = 0
        for physical_row, _row_group, _row_in_group, values in iter_rows(
            info, list(equality_ids), what="equality delete file"
        ):
            key = tuple(values[field_id] for field_id in equality_ids)
            record = DeleteRecord(
                delete_file_path=state.file_path,
                content=CONTENT_EQUALITY_DELETE,
                delete_sequence=state.data_sequence,
                physical_row=physical_row,
                reason="equality",
                key_values=key,
                key_names=tuple(key_names),
            )
            keys.setdefault(key, []).append(record)
            count += 1
        if count != state.record_count:
            raise InvalidTableError(
                f"equality delete file {state.file_path} has {count} rows, "
                f"manifest says {state.record_count}"
            )
        return _EqualityDeleteIndex(
            delete_sequence=state.data_sequence,
            field_ids=tuple(equality_ids),
            key_names=tuple(key_names),
            keys=keys,
        )
    finally:
        info.close()


def _validate_file_states(states: list[FileState]) -> None:
    # A live data file must not appear twice in a single snapshot's manifest
    # list; that would make "the same data file" ambiguous.
    live_paths: dict[str, FileState] = {}
    for state in states:
        if state.content == CONTENT_DATA and state.live:
            existing = live_paths.get(state.file_path)
            if existing is not None:
                raise InvalidTableError(
                    f"data file {state.file_path} appears twice as live in the snapshot"
                )
            live_paths[state.file_path] = state
        if state.data_sequence > state.file_sequence:
            raise InvalidTableError(
                f"file {state.file_path}: data sequence {state.data_sequence} "
                f"exceeds file sequence {state.file_sequence}"
            )


def plan_snapshot(
    plan: Plan,
    metadata: TableMetadata,
    snapshot_id: int,
    *,
    reader_factory=None,
) -> SnapshotPlan:
    snapshot = metadata.snapshot(snapshot_id)
    entries_and_states, manifest_list_path = collect_manifest_entries(
        plan, metadata, snapshot_id
    )
    states = [state for _entry, state, _manifest in entries_and_states]
    _validate_file_states(states)

    position_index = _PositionDeleteIndex(by_path={})
    equality_indices: list[_EqualityDeleteIndex] = []
    delete_files_read: list[str] = []

    for entry, state, _manifest in entries_and_states:
        if not state.live or state.content == CONTENT_DATA:
            continue
        if entry.data_file.content == CONTENT_POSITION_DELETE:
            for pos, record, target in _read_position_deletes(
                plan, state, reader_factory=reader_factory
            ):
                position_index.by_path.setdefault(target, []).append((pos, record))
        elif entry.data_file.content == CONTENT_EQUALITY_DELETE:
            equality_indices.append(
                _read_equality_deletes(
                    plan, state, metadata.schema, entry, reader_factory=reader_factory
                )
            )
        else:  # pragma: no cover - manifest parsing rejects this
            raise UnsupportedError(
                f"unsupported: delete content {entry.data_file.content}"
            )
        delete_files_read.append(state.file_path)

    rows: list[RowDecision] = []
    data_files_read: list[str] = []
    live_data_states = [s for s in states if s.visible_as_live_data]
    field_ids = [f.id for f in metadata.schema.fields]

    # Deterministic order: manifest order, then entry order.  Physical row
    # numbers are always relative to each original file.
    for state in live_data_states:
        path = plan.resolve(state.file_path)
        info = open_parquet(path, reader_factory=reader_factory)
        data_files_read.append(state.file_path)
        try:
            _check_data_file_schema(info, metadata.schema, state.file_path)
            if info.num_rows != state.record_count:
                raise InvalidTableError(
                    f"data file {state.file_path} has {info.num_rows} rows, "
                    f"manifest says {state.record_count}"
                )
            for physical_row, row_group, row_in_group, values in iter_rows(
                info, field_ids, what="data file"
            ):
                evidence: list[DeleteRecord] = []
                # Position deletes: exact data file identity and zero-based
                # physical row; eligible when data_sequence <= delete_sequence.
                for pos, record in position_index.by_path.get(state.file_path, ()):
                    if pos == physical_row and state.data_sequence <= record.delete_sequence:
                        evidence.append(record)
                # Equality deletes: eligible when data_sequence < delete_sequence
                # (strict).  NULL == NULL per the task's equality semantics.
                for eq in equality_indices:
                    if not (state.data_sequence < eq.delete_sequence):
                        continue
                    key = tuple(values[field_id] for field_id in eq.field_ids)
                    for record in eq.keys.get(key, ()):
                        evidence.append(record)
                values_by_name = {
                    schema_field.name: values[schema_field.id]
                    for schema_field in metadata.schema.fields
                }
                rows.append(
                    RowDecision(
                        snapshot_id=snapshot_id,
                        file_state=state,
                        physical_row=physical_row,
                        row_group=row_group,
                        row_in_group=row_in_group,
                        values_by_id=values,
                        values_by_name=values_by_name,
                        data_sequence=state.data_sequence,
                        file_sequence=state.file_sequence,
                        deleted=bool(evidence),
                        evidence=evidence,
                    )
                )
        finally:
            info.close()

    scan = SnapshotScan(
        snapshot_id=snapshot_id,
        sequence_number=snapshot.sequence_number,
        manifest_list_path=manifest_list_path,
        manifest_paths=tuple(
            dict.fromkeys(
                manifest.list_entry.manifest_path
                for _entry, _state, manifest in entries_and_states
            )
        ),
        file_states=tuple(states),
        rows=rows,
        data_files_read=tuple(data_files_read),
        delete_files_read=tuple(delete_files_read),
    )
    # Indices are kept only for in-process debugging; they are not written out.
    indices: dict[str, Any] = {
        "position": position_index,
        "equality": equality_indices,
    }
    return SnapshotPlan(scan=scan, schema=metadata.schema, delete_indices=indices)


def explain_row(
    plan: Plan,
    metadata: TableMetadata,
    snapshot_id: int,
    file_path: str,
    physical_row: int,
) -> dict[str, Any]:
    """Explain a single physical row by replaying the snapshot scan.

    The scan is deterministic and does not mutate any input file, so the
    explanation is derived from exactly the same code path used for output.
    """
    snapshot = metadata.snapshot(snapshot_id)
    planned = plan_snapshot(plan, metadata, snapshot_id)
    matching = [
        r
        for r in planned.scan.rows
        if r.file_state.file_path == file_path and r.physical_row == physical_row
    ]
    file_states = [s for s in planned.scan.file_states if s.file_path == file_path]
    if not matching:
        if not any(s.visible_as_live_data for s in file_states):
            statuses = ", ".join(
                f"{s.status_name}@{s.data_sequence}" for s in file_states
            ) or "absent from this snapshot's manifests"
            raise RowNotFoundError(
                f"data file {file_path} is not a live data file in snapshot "
                f"{snapshot_id} ({statuses})"
            )
        live = next(s for s in file_states if s.visible_as_live_data)
        raise RowNotFoundError(
            f"physical row {physical_row} is outside 0..{live.record_count - 1} "
            f"for {file_path} in snapshot {snapshot_id}"
        )
    row = matching[0]
    state = row.file_state
    return {
        "snapshot": {
            "snapshot_id": snapshot.snapshot_id,
            "sequence_number": snapshot.sequence_number,
            "manifest_list": snapshot.manifest_list,
            "summary": snapshot.metadata.get("summary", {}),
            "parent_snapshot_id": snapshot.metadata.get("parent-snapshot-id"),
        },
        "data_file": {
            "file_path": state.file_path,
            "manifest_path": state.manifest_path,
            "manifest_sequence_number": state.manifest_sequence_number,
            "manifest_entry_index": state.entry_index,
            "status": state.status_name,
            "entry_snapshot_id": state.entry_snapshot_id,
            "data_sequence": state.data_sequence,
            "file_sequence": state.file_sequence,
            "data_sequence_inherited": state.inherited_sequence,
            "file_sequence_inherited": state.inherited_file_sequence,
            "record_count": state.record_count,
        },
        "row": {
            "physical_row": physical_row,
            "row_group": row.row_group,
            "row_in_group": row.row_in_group,
            "values": row.values_by_name,
        },
        "visible": not row.deleted,
        "deleted": row.deleted,
        "delete_evidence": [e.as_dict() for e in row.evidence],
        "eligibility": {
            "position_delete_rule": "data_sequence <= delete_sequence",
            "equality_delete_rule": "data_sequence < delete_sequence",
            "note": "file_sequence is recorded but never used for delete eligibility",
        },
    }
