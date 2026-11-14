"""Avro manifest-list / manifest-entry reading and sequence inheritance."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator

import fastavro

from .errors import InvalidTableError, UnsupportedError
from .metadata import TableMetadata
from .plan import Plan

STATUS_EXISTING = 0
STATUS_ADDED = 1
STATUS_DELETED = 2
STATUS_NAMES = {0: "EXISTING", 1: "ADDED", 2: "DELETED"}

CONTENT_DATA = 0
CONTENT_POSITION_DELETE = 1
CONTENT_EQUALITY_DELETE = 2
CONTENT_NAMES = {0: "data", 1: "position_delete", 2: "equality_delete"}

MANIFEST_CONTENT_DATA = 0
MANIFEST_CONTENT_DELETES = 1


@dataclass(frozen=True)
class ManifestListEntry:
    manifest_path: str
    manifest_length: int
    partition_spec_id: int
    content: int
    sequence_number: int
    min_sequence_number: int
    added_snapshot_id: int
    raw: dict[str, Any]


@dataclass(frozen=True)
class DataFile:
    content: int
    file_path: str
    file_format: str
    record_count: int
    file_size_in_bytes: int
    equality_ids: tuple[int, ...] | None
    sort_order_id: int | None

    @property
    def is_data(self) -> bool:
        return self.content == CONTENT_DATA

    @property
    def is_position_delete(self) -> bool:
        return self.content == CONTENT_POSITION_DELETE

    @property
    def is_equality_delete(self) -> bool:
        return self.content == CONTENT_EQUALITY_DELETE

    @property
    def content_name(self) -> str:
        return CONTENT_NAMES.get(self.content, f"unknown({self.content})")


@dataclass(frozen=True)
class ManifestEntry:
    status: int
    snapshot_id: int | None
    sequence_number: int
    file_sequence_number: int
    data_file: DataFile
    manifest_path: str
    manifest_sequence_number: int
    inherited_sequence_number: bool
    inherited_file_sequence_number: bool
    raw: dict[str, Any]

    @property
    def status_name(self) -> str:
        return STATUS_NAMES.get(self.status, f"unknown({self.status})")

    @property
    def file_path(self) -> str:
        return self.data_file.file_path


@dataclass(frozen=True)
class Manifest:
    list_entry: ManifestListEntry
    entries: tuple[ManifestEntry, ...]

    @property
    def content_name(self) -> str:
        return CONTENT_NAMES.get(self.list_entry.content, f"unknown({self.list_entry.content})")


def _read_avro_records(path, plan: Plan, what: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        with path.open("rb") as fh:
            reader = fastavro.reader(fh)
            records = list(reader)
            metadata = dict(reader.metadata)
    except FileNotFoundError:
        raise InvalidTableError(f"{what} file does not exist: {path}") from None
    except Exception as exc:  # fastavro raises avro-ish exceptions
        raise InvalidTableError(f"cannot read {what} avro {path}: {exc}") from exc
    return metadata, records


def _validate_manifest_list_entry(entry: dict[str, Any], manifest_path: str, snapshot_id: int) -> ManifestListEntry:
    required_ints = (
        "manifest_length",
        "partition_spec_id",
        "content",
        "sequence_number",
        "min_sequence_number",
        "added_snapshot_id",
    )
    for key in required_ints:
        value = entry.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise InvalidTableError(
                f"snapshot {snapshot_id} manifest-list entry {manifest_path} has invalid {key}: {value!r}"
            )
    content = entry["content"]
    if content not in (MANIFEST_CONTENT_DATA, MANIFEST_CONTENT_DELETES):
        raise UnsupportedError(
            f"unsupported: manifest {manifest_path} has content {content!r}"
        )
    partition_spec_id = entry["partition_spec_id"]
    if partition_spec_id != 0:
        raise UnsupportedError(
            f"unsupported: manifest {manifest_path} uses partition spec {partition_spec_id!r}"
        )
    seq = entry["sequence_number"]
    if seq < 0:
        raise InvalidTableError(f"manifest {manifest_path} has negative sequence_number")
    min_seq = entry["min_sequence_number"]
    if min_seq < 0:
        raise InvalidTableError(f"manifest {manifest_path} has negative min_sequence_number")
    return ManifestListEntry(
        manifest_path=manifest_path,
        manifest_length=entry["manifest_length"],
        partition_spec_id=partition_spec_id,
        content=content,
        sequence_number=seq,
        min_sequence_number=min_seq,
        added_snapshot_id=entry["added_snapshot_id"],
        raw=entry,
    )


def read_manifest_list(plan: Plan, snapshot, metadata: TableMetadata) -> list[ManifestListEntry]:
    path = plan.resolve(snapshot.manifest_list)
    _, records = _read_avro_records(path, plan, f"manifest-list for snapshot {snapshot.snapshot_id}")
    entries: list[ManifestListEntry] = []
    if not records:
        return entries
    for record in records:
        if not isinstance(record, dict):
            raise InvalidTableError("manifest-list record is not an object")
        manifest_path = record.get("manifest_path")
        if not isinstance(manifest_path, str) or not manifest_path:
            raise InvalidTableError("manifest-list entry has no manifest_path")
        entry = _validate_manifest_list_entry(record, manifest_path, snapshot.snapshot_id)
        entries.append(entry)
    return entries


def read_manifest(plan: Plan, list_entry: ManifestListEntry) -> Manifest:
    path = plan.resolve(list_entry.manifest_path)
    avro_metadata, records = _read_avro_records(
        path, plan, f"manifest {list_entry.manifest_path}"
    )

    fmt = avro_metadata.get("format-version")
    if fmt is not None and str(fmt) != "2":
        raise UnsupportedError(
            f"unsupported: manifest {list_entry.manifest_path} format-version {fmt!r}"
        )
    declared_content = avro_metadata.get("content")
    if declared_content is not None:
        expected = (
            "data" if list_entry.content == MANIFEST_CONTENT_DATA else "deletes"
        )
        if declared_content != expected:
            raise InvalidTableError(
                f"manifest {list_entry.manifest_path} declares content {declared_content!r} "
                f"but manifest-list says {expected!r}"
            )
    declared_spec = avro_metadata.get("partition-spec-id")
    if declared_spec is not None and str(declared_spec) != "0":
        raise UnsupportedError(
            f"unsupported: manifest {list_entry.manifest_path} partition-spec-id {declared_spec!r}"
        )
    try:
        partition_text = avro_metadata.get("partition-spec")
        if partition_text is not None:
            spec = json.loads(partition_text)
            if spec:
                raise UnsupportedError(
                    f"unsupported: manifest {list_entry.manifest_path} has a non-empty partition spec"
                )
    except json.JSONDecodeError:
        raise InvalidTableError(
            f"manifest {list_entry.manifest_path} has unreadable partition-spec metadata"
        ) from None

    entries: list[ManifestEntry] = []
    for record in records:
        if not isinstance(record, dict):
            raise InvalidTableError(
                f"manifest {list_entry.manifest_path} contains a non-object entry"
            )
        entries.append(
            _build_entry(record, list_entry, path)
        )
    return Manifest(list_entry, tuple(entries))


def _build_entry(record: dict[str, Any], list_entry: ManifestListEntry, manifest_path) -> ManifestEntry:
    status = record.get("status")
    if status not in STATUS_NAMES:
        raise UnsupportedError(f"unsupported: manifest entry status {status!r}")
    raw_seq = record.get("sequence_number")
    raw_file_seq = record.get("file_sequence_number")

    # Per the Iceberg v2 spec, ADDED entries may inherit their sequence
    # number from the manifest-list entry.  In this fixture all nulls are ADDED,
    # and a null on any other status is a malformed table.
    inherited_seq = False
    if raw_seq is None:
        if status != STATUS_ADDED:
            raise InvalidTableError(
                f"manifest {list_entry.manifest_path} has a null sequence_number on a "
                f"{STATUS_NAMES.get(status)} entry"
            )
        seq = list_entry.sequence_number
        inherited_seq = True
    else:
        if not isinstance(raw_seq, int) or isinstance(raw_seq, bool) or raw_seq < 0:
            raise InvalidTableError(
                f"manifest {list_entry.manifest_path} has invalid sequence_number {raw_seq!r}"
            )
        seq = raw_seq

    inherited_file_seq = False
    if raw_file_seq is None:
        if status != STATUS_ADDED:
            raise InvalidTableError(
                f"manifest {list_entry.manifest_path} has a null file_sequence_number on a "
                f"{STATUS_NAMES.get(status)} entry"
            )
        file_seq = list_entry.sequence_number
        inherited_file_seq = True
    else:
        if not isinstance(raw_file_seq, int) or isinstance(raw_file_seq, bool) or raw_file_seq < 0:
            raise InvalidTableError(
                f"manifest {list_entry.manifest_path} has invalid file_sequence_number {raw_file_seq!r}"
            )
        file_seq = raw_file_seq

    if seq > file_seq:
        raise InvalidTableError(
            f"manifest {list_entry.manifest_path}: data sequence {seq} exceeds file sequence {file_seq}"
        )

    data_file_raw = record.get("data_file")
    if not isinstance(data_file_raw, dict):
        raise InvalidTableError(f"manifest {list_entry.manifest_path} entry has no data_file object")
    data_file = _build_data_file(data_file_raw, list_entry)

    snapshot_id = record.get("snapshot_id")
    if snapshot_id is not None and (
        not isinstance(snapshot_id, int) or isinstance(snapshot_id, bool)
    ):
        raise InvalidTableError(
            f"manifest {list_entry.manifest_path} has invalid snapshot_id {snapshot_id!r}"
        )

    return ManifestEntry(
        status=status,
        snapshot_id=snapshot_id,
        sequence_number=seq,
        file_sequence_number=file_seq,
        data_file=data_file,
        manifest_path=list_entry.manifest_path,
        manifest_sequence_number=list_entry.sequence_number,
        inherited_sequence_number=inherited_seq,
        inherited_file_sequence_number=inherited_file_seq,
        raw=record,
    )


def _build_data_file(raw: dict[str, Any], list_entry: ManifestListEntry) -> DataFile:
    content = raw.get("content")
    if content not in CONTENT_NAMES:
        raise UnsupportedError(f"unsupported: data_file content {content!r}")
    path = raw.get("file_path")
    file_format = raw.get("file_format")
    if not isinstance(path, str) or not path:
        raise InvalidTableError("data_file has no file_path")
    if not isinstance(file_format, str) or not file_format:
        raise InvalidTableError("data_file has no file_format")
    if file_format.upper() != "PARQUET":
        raise UnsupportedError(
            f"unsupported: file format {file_format!r} for {path}; only PARQUET is supported"
        )
    record_count = raw.get("record_count")
    file_size = raw.get("file_size_in_bytes")
    if not isinstance(record_count, int) or isinstance(record_count, bool) or record_count < 0:
        raise InvalidTableError(f"data_file {path} has invalid record_count {record_count!r}")
    if not isinstance(file_size, int) or isinstance(file_size, bool) or file_size < 0:
        raise InvalidTableError(f"data_file {path} has invalid file_size_in_bytes {file_size!r}")

    if content == CONTENT_DATA and raw.get("equality_ids") is not None:
        raise InvalidTableError(f"data file {path} must not have equality_ids")
    if content != CONTENT_EQUALITY_DELETE and raw.get("equality_ids") is not None:
        raise InvalidTableError(f"delete file {path} of content {content} must not have equality_ids")
    if content == CONTENT_EQUALITY_DELETE and raw.get("equality_ids") is None:
        raise InvalidTableError(f"equality delete file {path} must declare equality_ids")

    equality_ids_raw = raw.get("equality_ids")
    equality_ids: tuple[int, ...] | None = None
    if equality_ids_raw is not None:
        if not isinstance(equality_ids_raw, list) or not equality_ids_raw:
            raise InvalidTableError(f"equality delete file {path} has empty/invalid equality_ids")
        ids: list[int] = []
        for value in equality_ids_raw:
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise InvalidTableError(
                    f"equality delete file {path} has invalid equality id {value!r}"
                )
            ids.append(value)
        if len(set(ids)) != len(ids):
            raise InvalidTableError(f"equality delete file {path} repeats equality ids")
        equality_ids = tuple(ids)

    for key in ("partition",):
        if key in raw and raw[key]:
            raise UnsupportedError(
                f"unsupported: data_file {path} has a non-empty partition tuple"
            )

    return DataFile(
        content=content,
        file_path=path,
        file_format=file_format,
        record_count=record_count,
        file_size_in_bytes=file_size,
        equality_ids=equality_ids,
        sort_order_id=raw.get("sort_order_id"),
    )


def iter_snapshot_manifests(
    plan: Plan, snapshot, metadata: TableMetadata
) -> Iterator[Manifest]:
    for list_entry in read_manifest_list(plan, snapshot, metadata):
        yield read_manifest(plan, list_entry)
