"""Table metadata parsing and validation for the supported v2 subset."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import InvalidTableError, UnsupportedError
from .plan import Plan

_SUPPORTED_TYPES = {"long", "string"}


@dataclass(frozen=True)
class SchemaField:
    id: int
    name: str
    type: str
    required: bool


@dataclass(frozen=True)
class TableSchema:
    schema_id: int
    fields: tuple[SchemaField, ...]

    @property
    def field_ids(self) -> frozenset[int]:
        return frozenset(f.id for f in self.fields)

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    def by_id(self, field_id: int) -> SchemaField:
        for field in self.fields:
            if field.id == field_id:
                return field
        raise UnsupportedError(f"unsupported: schema has no field id {field_id}")

    def by_name(self, name: str) -> SchemaField:
        for field in self.fields:
            if field.name == name:
                return field
        raise UnsupportedError(f"unsupported: schema has no field named {name!r}")


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: int
    sequence_number: int
    timestamp_ms: int
    manifest_list: str
    schema_id: int
    metadata: dict[str, Any]


@dataclass(frozen=True)
class TableMetadata:
    path: Path
    format_version: int
    location: str
    table_uuid: str
    last_sequence_number: int
    schema: TableSchema
    snapshots: dict[int, Snapshot]
    partition_specs: tuple[dict[str, Any], ...]
    default_spec_id: int
    current_snapshot_id: int | None
    raw: dict[str, Any]

    def snapshot(self, snapshot_id: int) -> Snapshot:
        try:
            return self.snapshots[snapshot_id]
        except KeyError:
            raise InvalidTableError(
                f"snapshot {snapshot_id} is not present in table metadata"
            ) from None

    def local_table_root(self, plan: Plan) -> Path:
        """Resolve the table location to a local root through the fixed mapping."""
        ref = self.location.rstrip("/") + "/"
        return plan.resolve(ref)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidTableError(message)


def _unsupported(message: str) -> UnsupportedError:
    return UnsupportedError(f"unsupported: {message}")


def load_table_metadata(plan: Plan) -> TableMetadata:
    path = plan.resolve(plan.metadata_ref)
    if not path.is_file():
        raise InvalidTableError(f"metadata file does not exist: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidTableError(f"cannot read metadata json {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise InvalidTableError("table metadata must be a JSON object")

    format_version = raw.get("format-version")
    if format_version != 2:
        raise _unsupported(f"format-version {format_version!r}; only v2 is supported")

    location = raw.get("location")
    if not isinstance(location, str) or not location:
        raise InvalidTableError("table metadata is missing 'location'")

    schemas_raw = raw.get("schemas")
    if not isinstance(schemas_raw, list) or not schemas_raw:
        raise InvalidTableError("table metadata is missing 'schemas'")
    if len(schemas_raw) > 1:
        raise _unsupported("multiple schemas / schema evolution is not supported")
    schema_raw = schemas_raw[0]
    if not isinstance(schema_raw, dict):
        raise InvalidTableError("schema entry must be an object")
    schema_id = schema_raw.get("schema-id")
    if schema_id != 0:
        raise _unsupported(f"schema-id {schema_id!r}; only schema-id 0 is supported")
    if schema_raw.get("type") != "struct":
        raise _unsupported("schema type is not a flat struct")
    current_schema_id = raw.get("current-schema-id")
    if current_schema_id != 0:
        raise _unsupported(f"current-schema-id {current_schema_id!r}; only 0 is supported")

    fields_raw = schema_raw.get("fields")
    if not isinstance(fields_raw, list):
        raise InvalidTableError("schema 'fields' must be a list")
    fields: list[SchemaField] = []
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    for item in fields_raw:
        if not isinstance(item, dict):
            raise InvalidTableError("schema field must be an object")
        field_type = item.get("type")
        if isinstance(field_type, dict) or isinstance(field_type, list):
            raise _unsupported(f"nested/complex field type for {item.get('name')!r}")
        if field_type not in _SUPPORTED_TYPES:
            raise _unsupported(f"field type {field_type!r}; only long/string are supported")
        field_id = item.get("id")
        name = item.get("name")
        required = item.get("required")
        if not isinstance(field_id, int) or isinstance(field_id, bool):
            raise InvalidTableError(f"field id must be an integer: {field_id!r}")
        if not isinstance(name, str) or not name:
            raise InvalidTableError(f"field name must be a non-empty string: {name!r}")
        if not isinstance(required, bool):
            raise InvalidTableError(f"field required must be boolean: {required!r}")
        if field_id in seen_ids:
            raise InvalidTableError(f"duplicate schema field id {field_id}")
        if name in seen_names:
            raise InvalidTableError(f"duplicate schema field name {name!r}")
        seen_ids.add(field_id)
        seen_names.add(name)
        fields.append(SchemaField(field_id, name, field_type, required))
    if not fields:
        raise InvalidTableError("schema has no fields")
    schema = TableSchema(schema_id, tuple(fields))

    specs_raw = raw.get("partition-specs")
    if not isinstance(specs_raw, list):
        raise InvalidTableError("table metadata is missing 'partition-specs'")
    for spec in specs_raw:
        if not isinstance(spec, dict):
            raise InvalidTableError("partition spec must be an object")
        fields_list = spec.get("fields")
        if not isinstance(fields_list, list):
            raise InvalidTableError("partition spec fields must be a list")
        if fields_list:
            raise _unsupported("partitioned tables are not supported")
    default_spec_id = raw.get("default-spec-id", 0)
    if default_spec_id != 0:
        raise _unsupported(f"default-spec-id {default_spec_id!r}; only 0 is supported")

    snapshots_raw = raw.get("snapshots")
    if not isinstance(snapshots_raw, list) or not snapshots_raw:
        raise InvalidTableError("table metadata has no snapshots")
    snapshots: dict[int, Snapshot] = {}
    for item in snapshots_raw:
        if not isinstance(item, dict):
            raise InvalidTableError("snapshot entry must be an object")
        sid = item.get("snapshot-id")
        seq = item.get("sequence-number")
        ts = item.get("timestamp-ms")
        manifest_list = item.get("manifest-list")
        snap_schema = item.get("schema-id", 0)
        if not isinstance(sid, int) or isinstance(sid, bool):
            raise InvalidTableError(f"snapshot-id must be an integer: {sid!r}")
        if sid in snapshots:
            raise InvalidTableError(f"duplicate snapshot-id {sid}")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise InvalidTableError(f"snapshot {sid} has invalid sequence-number {seq!r}")
        if not isinstance(ts, int) or isinstance(ts, bool):
            raise InvalidTableError(f"snapshot {sid} has invalid timestamp-ms {ts!r}")
        if not isinstance(manifest_list, str) or not manifest_list:
            raise InvalidTableError(f"snapshot {sid} has no manifest-list")
        if snap_schema != 0:
            raise _unsupported(
                f"snapshot {sid} uses schema-id {snap_schema!r}; schema evolution is not supported"
            )
        snapshots[sid] = Snapshot(sid, seq, ts, manifest_list, 0, item)

    current = raw.get("current-snapshot-id")
    if current is not None and current not in snapshots:
        raise InvalidTableError(
            f"current-snapshot-id {current} is not in metadata snapshots"
        )

    last_seq = raw.get("last-sequence-number", 0)
    if not isinstance(last_seq, int) or isinstance(last_seq, bool) or last_seq < 0:
        raise InvalidTableError(f"invalid last-sequence-number {last_seq!r}")

    table_uuid = raw.get("table-uuid", "")
    if not isinstance(table_uuid, str):
        raise InvalidTableError("table-uuid must be a string")

    return TableMetadata(
        path=path,
        format_version=format_version,
        location=location,
        table_uuid=table_uuid,
        last_sequence_number=last_seq,
        schema=schema,
        snapshots=snapshots,
        partition_specs=tuple(specs_raw),
        default_spec_id=default_spec_id,
        current_snapshot_id=current,
        raw=raw,
    )
