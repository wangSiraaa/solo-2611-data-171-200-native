"""Small Parquet helper that maps columns by Iceberg field id.

Only flat schemas are supported.  The scanner never selects columns by business
name: it always resolves the schema field id to the physical Parquet column and
then reads that column.  This is what makes equality-delete keys trustworthy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pyarrow as pa
import pyarrow.parquet as pq

from .errors import InvalidTableError, UnsupportedError


@dataclass(frozen=True)
class ParquetField:
    field_id: int
    name: str
    arrow_type: pa.DataType
    nullable: bool


@dataclass
class ParquetInfo:
    path: Path
    pf: pq.ParquetFile
    num_rows: int
    num_row_groups: int
    fields: tuple[ParquetField, ...]
    by_id: dict[int, ParquetField]

    def row_group_sizes(self) -> list[int]:
        return [
            self.pf.metadata.row_group(i).num_rows
            for i in range(self.num_row_groups)
        ]

    def close(self) -> None:
        try:
            self.pf.close()
        except Exception:
            pass


_FIELD_ID_RE = re.compile(r"field_id=(-?\d+)\s+(.*?)(?:\s+\(.*\))?;\s*$")


def _fallback_field_ids_from_text(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in text.splitlines():
        match = _FIELD_ID_RE.search(line)
        if not match:
            continue
        field_id = int(match.group(1))
        name = match.group(2).strip()
        # The root group line does not end in ';' and is therefore skipped.
        if name:
            result[name] = field_id
    return result


def _arrow_field_id(field: pa.Field) -> int | None:
    metadata = field.metadata or {}
    value = metadata.get(b"PARQUET:field_id")
    if value is None:
        return None
    try:
        return int(value.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        raise InvalidTableError(f"invalid PARQUET:field_id metadata on column {field.name!r}") from None


def open_parquet(path: Path, reader_factory=None) -> ParquetInfo:
    """Open a Parquet file and describe its flat columns by field id.

    ``reader_factory`` exists so tests can audit exactly which paths the
    scanner opens; production passes None and gets ``pq.ParquetFile``.
    """
    factory = reader_factory or pq.ParquetFile
    try:
        pf = factory(str(path))
    except FileNotFoundError:
        raise InvalidTableError(f"parquet file does not exist: {path}") from None
    except Exception as exc:
        raise InvalidTableError(f"cannot open parquet file {path}: {exc}") from exc

    try:
        arrow_schema = pf.schema_arrow
        text_ids = _fallback_field_ids_from_text(str(pf.schema))
        fields: list[ParquetField] = []
        seen_ids: set[int] = set()
        for field in arrow_schema:
            field_id = _arrow_field_id(field)
            if field_id is None:
                field_id = text_ids.get(field.name)
            if field_id is None:
                raise UnsupportedError(
                    f"unsupported: parquet column {field.name!r} in {path} has no field_id"
                )
            if field_id in seen_ids:
                raise InvalidTableError(
                    f"parquet file {path} repeats field_id {field_id}"
                )
            seen_ids.add(field_id)
            if pa.types.is_nested(field.type):
                # Nested values are outside the flat fixed-schema subset.
                raise UnsupportedError(
                    f"unsupported: nested parquet column {field.name!r} (field id {field_id}) in {path}"
                )
            fields.append(
                ParquetField(
                    field_id=field_id,
                    name=field.name,
                    arrow_type=field.type,
                    nullable=field.nullable,
                )
            )
        info = ParquetInfo(
            path=path,
            pf=pf,
            num_rows=pf.metadata.num_rows,
            num_row_groups=pf.metadata.num_row_groups,
            fields=tuple(fields),
            by_id={f.field_id: f for f in fields},
        )
        return info
    except Exception:
        try:
            pf.close()
        except Exception:
            pass
        raise


def require_field(
    info: ParquetInfo,
    field_id: int,
    expected_type: str,
    *,
    what: str,
) -> ParquetField:
    field = info.by_id.get(field_id)
    if field is None:
        raise UnsupportedError(
            f"unsupported: {what} {info.path} has no column with field_id={field_id}"
        )
    if expected_type == "long":
        if not pa.types.is_int64(field.arrow_type):
            raise InvalidTableError(
                f"{what} {info.path} field_id={field_id} is {field.arrow_type}, expected int64/long"
            )
    elif expected_type == "string":
        if not (pa.types.is_string(field.arrow_type) or pa.types.is_large_string(field.arrow_type)):
            raise InvalidTableError(
                f"{what} {info.path} field_id={field_id} is {field.arrow_type}, expected string"
            )
    else:  # pragma: no cover - callers only pass long/string
        raise AssertionError(expected_type)
    return field


def reject_unknown_fields(
    info: ParquetInfo,
    allowed_ids: set[int],
    *,
    what: str,
) -> None:
    extras = sorted(set(info.by_id) - allowed_ids)
    if extras:
        raise UnsupportedError(
            f"unsupported: {what} {info.path} contains column field id(s) "
            f"{extras} that are not in the table schema"
        )


def iter_rows(
    info: ParquetInfo,
    field_ids: list[int],
    *,
    what: str,
) -> Iterator[tuple[int, int, int, dict[int, Any]]]:
    """Yield ``(physical_row, row_group, row_in_group, values_by_id)``.

    ``physical_row`` is zero based across the whole file, not reset per row
    group.  Reading row groups in order is essential for position deletes.
    """
    fields: list[ParquetField] = []
    for field_id in field_ids:
        field = info.by_id.get(field_id)
        if field is None:
            raise UnsupportedError(
                f"unsupported: {what} {info.path} has no column with field_id={field_id}"
            )
        fields.append(field)
    names = [f.name for f in fields]
    if len(set(names)) != len(names):
        raise InvalidTableError(f"{what} {info.path} has duplicate physical column names")

    base = 0
    for row_group in range(info.num_row_groups):
        try:
            table = info.pf.read_row_group(row_group, columns=names)
        except Exception as exc:
            raise InvalidTableError(
                f"cannot read row group {row_group} of {what} {info.path}: {exc}"
            ) from exc
        count = table.num_rows
        columns: dict[int, list[Any]] = {}
        for field in fields:
            columns[field.field_id] = table.column(field.name).to_pylist()
        if count and any(len(values) != count for values in columns.values()):
            raise InvalidTableError(
                f"column length mismatch while reading {what} {info.path}"
            )
        for row_in_group in range(count):
            values = {
                field_id: column[row_in_group]
                for field_id, column in columns.items()
            }
            yield base + row_in_group, row_group, row_in_group, values
        base += count

    if base != info.num_rows:
        raise InvalidTableError(
            f"{what} {info.path}: read {base} rows but metadata says {info.num_rows}"
        )
