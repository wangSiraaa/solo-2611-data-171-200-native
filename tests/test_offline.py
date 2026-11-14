"""Tests for the offline Iceberg v2 scanner.

Run with::

    python -m unittest discover -s tests -v

Two kinds of checks live here:

1. Assertions against the provided three-snapshot fixture.
2. An independent replay in :func:`replay_snapshot` that re-reads metadata,
   manifests, and Parquet files without using any scanner code, then compares
   the multiset of visible/deleted rows and per-row provenance.
3. A synthetic table written at test time that exercises duplicate delete
   evidence and the data==delete sequence boundary.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

import fastavro
import pyarrow as pa
import pyarrow.parquet as pq

from iceberg_offline.errors import (
    InvalidTableError,
    ResolutionError,
    RowNotFoundError,
    UnsupportedError,
)
from iceberg_offline.manifest import (
    CONTENT_DATA,
    CONTENT_EQUALITY_DELETE,
    CONTENT_POSITION_DELETE,
)
from iceberg_offline.metadata import load_table_metadata
from iceberg_offline.parquet_io import (
    open_parquet,
    reject_unknown_fields,
    require_field,
)
from iceberg_offline.plan import Plan
from iceberg_offline.scan import explain_row, plan_snapshot
from iceberg_offline.output import write_snapshot_outputs

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
PLAN_PATH = FIXTURES / "plan.json"
ORACLE_POS_PATH_ID = 2147483546
ORACLE_POS_ID = 2147483545


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fixture_hashes() -> dict[str, str]:
    return {
        str(p.relative_to(FIXTURES)): _sha256(p)
        for p in sorted(FIXTURES.rglob("*"))
        if p.is_file()
    }


def _plan_with_temp_mapping(tmp: Path, table_prefix: str = "table/") -> tuple[Plan, Path]:
    plan_dir = tmp
    plan = {
        "metadata": "table/metadata/v1.metadata.json",
        "uri_mapping": {"s3://offline-fixture/table/": table_prefix},
        "snapshots": [1, 2, 3],
        "supported": "unpartitioned Iceberg v2; flat fixed int64/string schema; data, position deletes, equality deletes",
    }
    plan_path = plan_dir / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return Plan.load(plan_path), plan_dir


def _write_parquet(path: Path, columns: dict[str, list[Any]], field_ids: dict[str, int]) -> None:
    arrays = []
    fields = []
    for name, values in columns.items():
        if name in ("id", "amount", "pos"):
            arrow_type = pa.int64()
        else:
            arrow_type = pa.string()
        arrays.append(pa.array(values, type=arrow_type))
        fields.append(
            pa.field(
                name,
                arrow_type,
                nullable=any(v is None for v in values),
                metadata={b"PARQUET:field_id": str(field_ids[name]).encode()},
            )
        )
    table = pa.Table.from_arrays(arrays, schema=pa.schema(fields))
    # Explicit row groups make cross-row-group physical numbering testable.
    pq.write_table(table, str(path), row_group_size=2)


def _template(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return copy.deepcopy(fastavro.reader(fh).writer_schema)


def _write_manifest(path: Path, schema: dict[str, Any], records: list[dict], content: str) -> None:
    metadata = {
        "format-version": "2",
        "partition-spec": "[]",
        "partition-spec-id": "0",
        "content": content,
        "avro.codec": "null",
        "schema": json.dumps(TABLE_SCHEMA),
    }
    with path.open("wb") as fh:
        fastavro.writer(fh, schema, records, codec="null", metadata=metadata)


def _write_manifest_list(path: Path, schema: dict[str, Any], snapshot_id: int, seq: int, records: list[dict]) -> None:
    metadata = {
        "snapshot-id": str(snapshot_id),
        "parent-snapshot-id": "null",
        "sequence-number": str(seq),
        "format-version": "2",
        "avro.codec": "null",
    }
    with path.open("wb") as fh:
        fastavro.writer(fh, schema, records, codec="null", metadata=metadata)


TABLE_SCHEMA = {
    "type": "struct",
    "schema-id": 0,
    "fields": [
        {"id": 1, "name": "id", "required": True, "type": "long"},
        {"id": 2, "name": "tag", "required": False, "type": "string"},
        {"id": 3, "name": "amount", "required": True, "type": "long"},
    ],
}


def _data_file_record(content: int, path: str, rows: int, equality_ids=None) -> dict:
    return {
        "content": content,
        "file_path": path,
        "file_format": "PARQUET",
        "partition": {},
        "record_count": rows,
        "file_size_in_bytes": 123,
        "equality_ids": equality_ids,
        "sort_order_id": None,
    }


def _entry(status: int, snapshot_id, seq, file_seq, data_file) -> dict:
    return {
        "status": status,
        "snapshot_id": snapshot_id,
        "sequence_number": seq,
        "file_sequence_number": file_seq,
        "data_file": data_file,
    }


def _manifest_list_entry(manifest_path: str, content: int, seq: int, added_snapshot_id: int) -> dict:
    return {
        "manifest_path": manifest_path,
        "manifest_length": 1,
        "partition_spec_id": 0,
        "content": content,
        "sequence_number": seq,
        "min_sequence_number": 0,
        "added_snapshot_id": added_snapshot_id,
        "added_files_count": None,
        "existing_files_count": None,
        "deleted_files_count": None,
        "added_rows_count": None,
        "existing_rows_count": None,
        "deleted_rows_count": None,
    }


def _snapshot_metadata(snap_id: int, seq: int, parent, manifest_list_uri: str, operation: str) -> dict:
    payload = {
        "snapshot-id": snap_id,
        "sequence-number": seq,
        "timestamp-ms": 1700000000000 + snap_id,
        "manifest-list": manifest_list_uri,
        "schema-id": 0,
        "summary": {"operation": operation},
    }
    if parent is not None:
        payload["parent-snapshot-id"] = parent
    return payload


# ---------------------------------------------------------------------------
# independent replay oracle (does not import scanner logic)
# ---------------------------------------------------------------------------


def replay_snapshot(plan_path: Path, snapshot_id: int) -> dict[str, Any]:
    """Replay the snapshot using only the format spec and raw readers.

    Returns ``{"scan": set(...), "visible": set(...), "deleted": set(...),
    "evidence": list(...)}`` where set elements are
    ``(file_path, physical_row, json_values)``.
    """
    plan = json.loads(plan_path.read_text())
    base = plan_path.parent
    metadata_path = base / plan["metadata"]
    metadata = json.loads(metadata_path.read_text())

    def resolve_local(uri: str) -> Path:
        for prefix, target in plan["uri_mapping"].items():
            if uri.startswith(prefix):
                return base / target / uri[len(prefix):]
        raise AssertionError(f"oracle: unresolved URI {uri}")

    snapshot = next(s for s in metadata["snapshots"] if s["snapshot-id"] == snapshot_id)
    schema_fields = metadata["schemas"][0]["fields"]
    field_ids = [f["id"] for f in schema_fields]
    field_names = {f["id"]: f["name"] for f in schema_fields}

    manifest_entries: list[dict] = []
    with resolve_local(snapshot["manifest-list"]).open("rb") as fh:
        reader = fastavro.reader(fh)
        for m in reader:
            list_seq = m["sequence_number"]
            with resolve_local(m["manifest_path"]).open("rb") as mf:
                mreader = fastavro.reader(mf)
                for e in mreader:
                    seq = e["sequence_number"]
                    file_seq = e["file_sequence_number"]
                    if seq is None:
                        seq = list_seq
                    if file_seq is None:
                        file_seq = list_seq
                    manifest_entries.append(
                        {
                            "status": e["status"],
                            "content": e["data_file"]["content"],
                            "path": e["data_file"]["file_path"],
                            "rows": e["data_file"]["record_count"],
                            "equality_ids": e["data_file"]["equality_ids"],
                            "seq": seq,
                            "file_seq": file_seq,
                            "manifest_content": m["content"],
                        }
                    )

    def read_columns(path_uri: str, ids: list[int]) -> list[dict]:
        # Deliberately does not use iceberg_offline.parquet_io: this oracle
        # must be able to disagree with the scanner.  Columns are still mapped
        # by field id as the format requires.
        path = resolve_local(path_uri)
        arrow = pq.ParquetFile(str(path)).schema_arrow
        by_id = {}
        for field in arrow:
            value = field.metadata.get(b"PARQUET:field_id")
            by_id[int(value)] = field.name
        table = pq.read_table(str(path), columns=[by_id[i] for i in ids])
        columns = {i: table.column(by_id[i]).to_pylist() for i in ids}
        return [{i: columns[i][r] for i in ids} for r in range(table.num_rows)]

    position_map: dict[tuple[str, int], list[tuple[int, str]]] = {}
    equality_sets: list[tuple[int, tuple[int, ...], dict[tuple, list]]] = []
    delete_files: list[str] = []
    for me in manifest_entries:
        if me["status"] not in (0, 1):
            continue
        if me["content"] == CONTENT_POSITION_DELETE:
            delete_files.append(me["path"])
            rows = read_columns(me["path"], [ORACLE_POS_PATH_ID, ORACLE_POS_ID])
            for pos, row in enumerate(rows):
                target = row[ORACLE_POS_PATH_ID]
                position_map.setdefault((target, row[ORACLE_POS_ID]), []).append(
                    (me["seq"], me["path"], pos)
                )
        elif me["content"] == CONTENT_EQUALITY_DELETE:
            delete_files.append(me["path"])
            ids = tuple(me["equality_ids"])
            rows = read_columns(me["path"], list(ids))
            keyed: dict[tuple, list] = {}
            for pos, row in enumerate(rows):
                key = tuple(row[i] for i in ids)
                keyed.setdefault(key, []).append((me["seq"], me["path"], pos))
            equality_sets.append((me["seq"], ids, keyed))

    scanned = set()
    visible = set()
    deleted = set()
    evidence: list[dict] = []
    data_files: list[str] = []
    for me in manifest_entries:
        if me["status"] not in (0, 1) or me["content"] != CONTENT_DATA:
            continue
        data_files.append(me["path"])
        rows = read_columns(me["path"], field_ids)
        assert len(rows) == me["rows"], "oracle: row count mismatch"
        for physical_row, values in enumerate(rows):
            ident = (
                me["path"],
                physical_row,
                json.dumps(
                    {field_names[i]: values[i] for i in field_ids}, sort_keys=True
                ),
            )
            scanned.add(ident)
            hits: list[dict] = []
            for (target, pos), dels in position_map.items():
                if target == me["path"] and pos == physical_row:
                    for delete_seq, dpath, drow in dels:
                        if me["seq"] <= delete_seq:
                            hits.append(
                                {
                                    "content": "position",
                                    "seq": delete_seq,
                                    "file": dpath,
                                    "delete_row": drow,
                                }
                            )
            for delete_seq, ids, keyed in equality_sets:
                if not (me["seq"] < delete_seq):
                    continue
                key = tuple(values[i] for i in ids)
                for dseq, dpath, drow in keyed.get(key, ()):
                    hits.append(
                        {
                            "content": "equality",
                            "seq": dseq,
                            "file": dpath,
                            "delete_row": drow,
                            "key_ids": list(ids),
                            "key_values": list(key),
                        }
                    )
            if hits:
                deleted.add(ident)
                evidence.append(
                    {
                        "file_path": me["path"],
                        "physical_row": physical_row,
                        "values": {field_names[i]: values[i] for i in field_ids},
                        "hits": hits,
                    }
                )
            else:
                visible.add(ident)
    return {
        "scan": scanned,
        "visible": visible,
        "deleted": deleted,
        "evidence": evidence,
        "data_files": data_files,
        "delete_files": delete_files,
    }


def _scanner_sets(plan: Plan, metadata, snapshot_id: int) -> dict[str, Any]:
    planned = plan_snapshot(plan, metadata, snapshot_id)
    scan = planned.scan
    visible = {
        (
            r.file_state.file_path,
            r.physical_row,
            json.dumps(r.values_by_name, sort_keys=True),
        )
        for r in scan.visible_rows
    }
    deleted = {
        (
            r.file_state.file_path,
            r.physical_row,
            json.dumps(r.values_by_name, sort_keys=True),
        )
        for r in scan.deleted_rows
    }
    scanned = visible | deleted
    evidence = [
        {
            "file_path": r.file_state.file_path,
            "physical_row": r.physical_row,
            "values": r.values_by_name,
            "hits": [e.as_dict() for e in r.evidence],
        }
        for r in scan.deleted_rows
    ]
    return {
        "scan": scanned,
        "visible": visible,
        "deleted": deleted,
        "evidence": evidence,
        "data_files": list(scan.data_files_read),
        "delete_files": list(scan.delete_files_read),
    }


# ---------------------------------------------------------------------------
# fixture tests
# ---------------------------------------------------------------------------


class FixtureScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = Plan.load(PLAN_PATH)
        cls.metadata = load_table_metadata(cls.plan)
        cls.before_hashes = _fixture_hashes()

    @classmethod
    def tearDownClass(cls):
        after = _fixture_hashes()
        assert after == cls.before_hashes, "fixture inputs changed during tests"

    def test_expected_visible_rows_per_snapshot(self):
        expected = {
            101: {(1, "x", 10), (2, None, 20), (3, "k", 30), (4, "x", 40), (5, "x", 10)},
            102: {(3, "k", 30), (7, None, 20), (8, "z", 0), (9, "x", 40)},
            103: {(8, "z", 0), (9, "x", 40), (10, "x", 10), (7, None, 20), (11, "q", 90)},
        }
        for snapshot_id, wanted in expected.items():
            planned = plan_snapshot(self.plan, self.metadata, snapshot_id)
            got = {
                (r.values_by_name["id"], r.values_by_name["tag"], r.values_by_name["amount"])
                for r in planned.scan.visible_rows
            }
            self.assertEqual(got, wanted, f"snapshot {snapshot_id}")

    def test_independent_replay_matches_multiset_and_evidence(self):
        for snapshot_id in self.plan.snapshots:
            oracle = replay_snapshot(PLAN_PATH, snapshot_id)
            ours = _scanner_sets(self.plan, self.metadata, snapshot_id)
            self.assertEqual(ours["scan"], oracle["scan"], f"scanned rows {snapshot_id}")
            self.assertEqual(ours["visible"], oracle["visible"], f"visible {snapshot_id}")
            self.assertEqual(ours["deleted"], oracle["deleted"], f"deleted {snapshot_id}")
            self.assertEqual(
                sorted(ours["data_files"]), sorted(oracle["data_files"]),
                f"data file set {snapshot_id}",
            )
            self.assertEqual(
                sorted(ours["delete_files"]), sorted(oracle["delete_files"]),
                f"delete file set {snapshot_id}",
            )

            # Evidence: same number of delete hits per (file, row), and the
            # same delete file / delete row / key for each hit.
            def normalise(entries, ours_style: bool):
                out = []
                for entry in entries:
                    hits = []
                    for hit in entry["hits"]:
                        if ours_style:
                            content = hit["content"].replace("_delete", "")
                            hits.append(
                                (
                                    content,
                                    hit["delete_file_path"],
                                    hit["physical_row"],
                                    tuple(hit["key_values"]) if hit["key_values"] else None,
                                )
                            )
                        else:
                            hits.append(
                                (
                                    "equality" if hit["content"] == "equality" else "position",
                                    hit["file"],
                                    hit["delete_row"] if "delete_row" in hit else None,
                                    tuple(hit["key_values"]) if "key_values" in hit else None,
                                )
                            )
                    out.append(
                        (
                            entry["file_path"],
                            entry["physical_row"],
                            tuple(sorted(hits, key=repr)),
                        )
                    )
                return sorted(out, key=repr)

            self.assertEqual(
                normalise(ours["evidence"], True),
                normalise(oracle["evidence"], False),
                f"delete evidence {snapshot_id}",
            )

    def test_sequence_inheritance_not_current_snapshot(self):
        for snapshot_id in (101, 102, 103):
            planned = plan_snapshot(self.plan, self.metadata, snapshot_id)
            states = {s.file_path.split("/")[-1]: s for s in planned.scan.file_states}
            # a is added at snapshot 101 with null sequence -> inherit 1.
            if snapshot_id == 101:
                self.assertEqual(states["a.parquet"].data_sequence, 1)
                self.assertTrue(states["a.parquet"].inherited_sequence)
                self.assertTrue(states["a.parquet"].inherited_file_sequence)
            if snapshot_id == 102:
                self.assertEqual(states["a.parquet"].data_sequence, 1)
                self.assertFalse(states["a.parquet"].inherited_sequence)
                self.assertEqual(states["b.parquet"].data_sequence, 2)
                self.assertTrue(states["b.parquet"].inherited_sequence)
                self.assertEqual(states["pos2.parquet"].data_sequence, 2)
                self.assertEqual(states["eq2.parquet"].data_sequence, 2)
            if snapshot_id == 103:
                self.assertEqual(states["a.parquet"].data_sequence, 1)
                self.assertEqual(states["a.parquet"].status_name, "DELETED")
                self.assertEqual(states["c.parquet"].data_sequence, 3)
                self.assertTrue(states["c.parquet"].inherited_sequence)
                self.assertEqual(states["eq3.parquet"].data_sequence, 3)
            for state in planned.scan.file_states:
                if state.file_path.endswith("a.parquet") and snapshot_id == 103:
                    self.assertNotEqual(
                        state.data_sequence,
                        planned.scan.sequence_number,
                        "each file must not be stamped with the snapshot sequence",
                    )

    def test_removed_and_unreferenced_files_are_not_read(self):
        opened: list[str] = []

        def factory(path):
            opened.append(str(path))
            return pq.ParquetFile(path)

        planned = plan_snapshot(self.plan, self.metadata, 103, reader_factory=factory)
        opened_names = [Path(p).name for p in opened]
        self.assertEqual(sorted(opened_names), ["b.parquet", "c.parquet", "eq2.parquet", "eq3.parquet", "pos2.parquet"])
        self.assertNotIn("a.parquet", opened_names)
        self.assertNotIn("unreferenced.parquet", opened_names)
        self.assertNotIn(
            "s3://offline-fixture/table/data/unreferenced.parquet",
            planned.scan.data_files_read,
        )
        self.assertNotIn(
            "s3://offline-fixture/table/data/a.parquet",
            planned.scan.data_files_read,
        )

    def test_position_delete_crosses_row_group_boundary(self):
        # a.parquet has row groups [rows 0-1][rows 2-3][row 4]; pos 3 must
        # resolve to the fourth physical row, not row 1 of its row group.
        planned = plan_snapshot(self.plan, self.metadata, 102)
        row = next(
            r
            for r in planned.scan.rows
            if r.file_state.file_path.endswith("a.parquet") and r.physical_row == 3
        )
        self.assertTrue(row.deleted)
        self.assertEqual(row.row_group, 1)
        self.assertEqual(row.row_in_group, 1)
        self.assertEqual(row.evidence[0].content, CONTENT_POSITION_DELETE)
        # Ensure the neighbour row in the same row group is unaffected.
        row2 = next(
            r
            for r in planned.scan.rows
            if r.file_state.file_path.endswith("a.parquet") and r.physical_row == 2
        )
        self.assertFalse(row2.deleted)

    def test_equality_delete_composite_and_null_semantics(self):
        # eq2 uses equality_ids [2, 3] = (tag, amount).  ('x',10) matches rows
        # 0 and 4, (None,20) matches row 1.  A name-based or id-based crash
        # would miss the NULL==NULL case.
        planned = plan_snapshot(self.plan, self.metadata, 102)
        deleted_a = sorted(
            r.physical_row
            for r in planned.scan.rows
            if r.file_state.file_path.endswith("a.parquet") and r.deleted
        )
        self.assertEqual(deleted_a, [0, 1, 3, 4])
        null_row = next(
            r
            for r in planned.scan.rows
            if r.file_state.file_path.endswith("a.parquet") and r.physical_row == 1
        )
        self.assertEqual(null_row.values_by_name["tag"], None)
        ev = null_row.evidence[0]
        self.assertEqual(ev.content, CONTENT_EQUALITY_DELETE)
        self.assertEqual(tuple(ev.key_values), (None, 20))
        self.assertEqual(tuple(ev.key_names), ("tag", "amount"))

    def test_same_sequence_rows_are_retained(self):
        # b file data_sequence 2; eq2 delete_sequence 2 is NOT eligible for
        # equality (strict <).  c file data_sequence 3; eq3 seq 3 not eligible.
        planned = plan_snapshot(self.plan, self.metadata, 102)
        b_rows = [
            r for r in planned.scan.rows if r.file_state.file_path.endswith("b.parquet")
        ]
        self.assertEqual([r.values_by_name["id"] for r in b_rows if r.deleted], [6])  # position only
        planned3 = plan_snapshot(self.plan, self.metadata, 103)
        c_rows = [
            r for r in planned3.scan.rows if r.file_state.file_path.endswith("c.parquet")
        ]
        self.assertFalse(any(r.deleted for r in c_rows))
        c_id7 = next(r for r in c_rows if r.values_by_name["id"] == 7)
        self.assertFalse(c_id7.deleted)  # same business value as deleted b row, new identity

    def test_zero_based_physical_rows_preserved(self):
        planned = plan_snapshot(self.plan, self.metadata, 102)
        for file_name in ("a.parquet", "b.parquet"):
            rows = sorted(
                (
                    r.physical_row,
                    r.row_group,
                    r.row_in_group,
                    r.values_by_name["id"],
                )
                for r in planned.scan.rows
                if r.file_state.file_path.endswith(file_name)
            )
            self.assertEqual([r[0] for r in rows], list(range(len(rows))))

    def test_explain_reports_chain_and_evidence(self):
        result = explain_row(
            self.plan,
            self.metadata,
            103,
            "s3://offline-fixture/table/data/b.parquet",
            1,
        )
        self.assertEqual(result["data_file"]["status"], "EXISTING")
        self.assertEqual(result["data_file"]["data_sequence"], 2)
        self.assertEqual(result["data_file"]["manifest_path"],
                         "s3://offline-fixture/table/metadata/m3-data.avro")
        self.assertEqual(result["snapshot"]["manifest_list"],
                         "s3://offline-fixture/table/metadata/snap-103.avro")
        self.assertTrue(result["deleted"])
        self.assertEqual(result["delete_evidence"][0]["delete_file_path"],
                         "s3://offline-fixture/table/data/eq3.parquet")
        self.assertEqual(result["delete_evidence"][0]["delete_sequence"], 3)

        result_visible = explain_row(
            self.plan,
            self.metadata,
            102,
            "s3://offline-fixture/table/data/b.parquet",
            2,
        )
        self.assertTrue(result_visible["visible"])
        self.assertEqual(result_visible["delete_evidence"], [])

    def test_explain_reports_removed_file(self):
        with self.assertRaises(RowNotFoundError) as ctx:
            explain_row(
                self.plan,
                self.metadata,
                103,
                "s3://offline-fixture/table/data/a.parquet",
                2,
            )
        self.assertIn("DELETED", str(ctx.exception))

    def test_outputs_are_independently_readable_and_preserve_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            planned = plan_snapshot(self.plan, self.metadata, 103)
            paths = write_snapshot_outputs(out, self.metadata.schema, planned.scan)
            table = pq.read_table(paths["rows"])
            self.assertEqual(table.num_rows, 5)
            self.assertEqual(
                [f.name for f in table.schema][:3], ["id", "tag", "amount"]
            )
            rows = table.to_pylist()
            for row in rows:
                self.assertIn("_iceberg_file_path", row)
                self.assertIn("_iceberg_physical_row", row)
                original = open_parquet(self.plan.resolve(row["_iceberg_file_path"]))
                self.assertEqual(
                    original.pf.metadata.num_rows > row["_iceberg_physical_row"], True
                )
                original.close()
            # files table lists DELETED entries and both sequence numbers.
            files_text = paths["files"].read_text()
            self.assertIn("DELETED,data,s3://offline-fixture/table/data/a.parquet,no,1,1", files_text)
            self.assertIn("ADDED,data,s3://offline-fixture/table/data/c.parquet,yes,3,3,yes,yes", files_text)
            # evidence lines retain original file identity and physical row.
            evidence = [
                json.loads(line)
                for line in paths["evidence"].read_text().splitlines()
            ]
            self.assertEqual(len(evidence), 7)
            deleted = [e for e in evidence if e["deleted"]]
            self.assertEqual(len(deleted), 2)
            deleted_ids = {e["values"]["id"] for e in deleted}
            self.assertEqual(deleted_ids, {6, 7})
            for entry in evidence:
                self.assertIn("physical_row", entry)
                self.assertIn("file_path", entry)

    def test_input_files_unchanged_after_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            for snapshot_id in self.plan.snapshots:
                write_snapshot_outputs(
                    Path(tmp) / str(snapshot_id),
                    self.metadata.schema,
                    plan_snapshot(self.plan, self.metadata, snapshot_id).scan,
                )
        self.assertEqual(_fixture_hashes(), self.before_hashes)


# ---------------------------------------------------------------------------
# unsupported / boundary diagnostics
# ---------------------------------------------------------------------------


class UnsupportedStructureTests(unittest.TestCase):
    def _copy_plan_tree(self, tmp: Path) -> Path:
        shutil.copytree(FIXTURES, tmp / "fixtures")
        return tmp / "fixtures" / "plan.json"

    def test_partitioned_metadata_is_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = self._copy_plan_tree(Path(tmp))
            meta_path = plan_path.parent / "table/metadata/v3.metadata.json"
            raw = json.loads(meta_path.read_text())
            raw["partition-specs"][0]["fields"] = [
                {"name": "p", "transform": "identity", "source-id": 1, "field-id": 1000}
            ]
            meta_path.write_text(json.dumps(raw))
            plan = Plan.load(plan_path)
            with self.assertRaises(UnsupportedError) as ctx:
                load_table_metadata(plan)
            self.assertTrue(str(ctx.exception).startswith("unsupported:"))

    def test_schema_evolution_is_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = self._copy_plan_tree(Path(tmp))
            meta_path = plan_path.parent / "table/metadata/v3.metadata.json"
            raw = json.loads(meta_path.read_text())
            extra = copy.deepcopy(raw["schemas"][0])
            extra["schema-id"] = 1
            raw["schemas"].append(extra)
            meta_path.write_text(json.dumps(raw))
            plan = Plan.load(plan_path)
            with self.assertRaises(UnsupportedError):
                load_table_metadata(plan)

    def test_v1_format_is_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = self._copy_plan_tree(Path(tmp))
            meta_path = plan_path.parent / "table/metadata/v3.metadata.json"
            raw = json.loads(meta_path.read_text())
            raw["format-version"] = 1
            meta_path.write_text(json.dumps(raw))
            with self.assertRaises(UnsupportedError):
                load_table_metadata(Plan.load(plan_path))

    def test_extra_parquet_field_id_is_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "extra.parquet"
            table = pa.Table.from_arrays(
                [
                    pa.array([1], type=pa.int64()),
                    pa.array(["x"], type=pa.string()),
                    pa.array([10], type=pa.int64()),
                    pa.array([True], type=pa.bool_()),
                ],
                schema=pa.schema(
                    [
                        pa.field("id", pa.int64(), metadata={b"PARQUET:field_id": b"1"}),
                        pa.field("tag", pa.string(), metadata={b"PARQUET:field_id": b"2"}),
                        pa.field("amount", pa.int64(), metadata={b"PARQUET:field_id": b"3"}),
                        pa.field("surprise", pa.bool_(), metadata={b"PARQUET:field_id": b"4"}),
                    ]
                ),
            )
            pq.write_table(table, str(path))
            info = open_parquet(path)
            try:
                with self.assertRaises(UnsupportedError):
                    reject_unknown_fields(info, {1, 2, 3}, what="data file")
                with self.assertRaises(InvalidTableError):
                    require_field(info, 1, "string", what="data file")
            finally:
                info.close()

    def test_unmapped_uri_is_rejected(self):
        plan = Plan.load(PLAN_PATH)
        with self.assertRaises(ResolutionError):
            plan.resolve("s3://other-bucket/table/data/a.parquet")

    def test_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "metadata": "table/metadata/v3.metadata.json",
                        "uri_mapping": {"s3://offline-fixture/": "../outside/"},
                        "snapshots": [],
                    }
                )
            )
            with self.assertRaises(ResolutionError):
                Plan.load(plan_path)

    def test_unknown_snapshot_in_metadata(self):
        plan = Plan.load(PLAN_PATH)
        metadata = load_table_metadata(plan)
        with self.assertRaises(InvalidTableError):
            metadata.snapshot(999)


# ---------------------------------------------------------------------------
# synthetic table: duplicate evidence and sequence boundary
# ---------------------------------------------------------------------------


class SyntheticBoundaryTests(unittest.TestCase):
    """Build a tiny table at test time to prove duplicate delete evidence.

    Snapshots:
      1: d1 added (data_sequence 1, inherits from manifest list)
      2: d1 EXISTING (1), d2 ADDED (2); position deletes for (d1,1), (d2,0),
         (d2,0) again; equality delete on id for d1 row 1 and d2 row 1.
         -> d2 row 1 has data_sequence == equality delete sequence, must stay.
      3: d1 DELETED, d2 EXISTING; same delete manifests.
         -> d1 is not read at all.
    """

    PREFIX = "s3://offline-fixture/table/"
    D1 = PREFIX + "data/d1.parquet"
    D2 = PREFIX + "data/d2.parquet"
    UNREF = PREFIX + "data/unreferenced.parquet"
    POS = PREFIX + "data/del-pos.parquet"
    EQ = PREFIX + "data/del-eq.parquet"

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls.tmpdir.name)
        table = cls.tmp / "table"
        (table / "data").mkdir(parents=True)
        (table / "metadata").mkdir(parents=True)

        _write_parquet(
            table / "data/d1.parquet",
            {"id": [1, 2, 3], "tag": ["a", None, "c"], "amount": [10, 20, 30]},
            {"id": 1, "tag": 2, "amount": 3},
        )
        _write_parquet(
            table / "data/d2.parquet",
            {"id": [4, 5], "tag": ["d", "e"], "amount": [40, 50]},
            {"id": 1, "tag": 2, "amount": 3},
        )
        _write_parquet(
            table / "data/unreferenced.parquet",
            {"id": [999], "tag": ["nope"], "amount": [9]},
            {"id": 1, "tag": 2, "amount": 3},
        )
        _write_parquet(
            table / "data/del-pos.parquet",
            {"file_path": [cls.D2, cls.D2, cls.D1], "pos": [0, 0, 1]},
            {"file_path": ORACLE_POS_PATH_ID, "pos": ORACLE_POS_ID},
        )
        _write_parquet(
            table / "data/del-eq.parquet",
            {"id": [2, 5]},
            {"id": 1},
        )

        entry_schema = _template(FIXTURES / "table/metadata/m1-data.avro")
        list_schema = _template(FIXTURES / "table/metadata/snap-101.avro")
        cls._entry_schema = entry_schema
        cls._list_schema = list_schema

        m1 = [
            _entry(1, 1, None, None, _data_file_record(0, cls.D1, 3)),
        ]
        _write_manifest(table / "metadata/m1.avro", entry_schema, m1, "data")

        m2 = [
            _entry(0, 1, 1, 1, _data_file_record(0, cls.D1, 3)),
            _entry(1, 2, None, None, _data_file_record(0, cls.D2, 2)),
        ]
        _write_manifest(table / "metadata/m2.avro", entry_schema, m2, "data")
        m2pos = [_entry(1, 2, None, None, _data_file_record(1, cls.POS, 3))]
        _write_manifest(table / "metadata/m2-pos.avro", entry_schema, m2pos, "deletes")
        m2eq = [_entry(1, 2, None, None, _data_file_record(2, cls.EQ, 2, equality_ids=[1]))]
        _write_manifest(table / "metadata/m2-eq.avro", entry_schema, m2eq, "deletes")

        m3 = [
            _entry(2, 3, 1, 1, _data_file_record(0, cls.D1, 3)),
            _entry(0, 2, 2, 2, _data_file_record(0, cls.D2, 2)),
        ]
        _write_manifest(table / "metadata/m3.avro", entry_schema, m3, "data")

        snap1 = [_manifest_list_entry(cls.PREFIX + "metadata/m1.avro", 0, 1, 1)]
        snap2 = [
            _manifest_list_entry(cls.PREFIX + "metadata/m2.avro", 0, 2, 1),
            _manifest_list_entry(cls.PREFIX + "metadata/m2-pos.avro", 1, 2, 2),
            _manifest_list_entry(cls.PREFIX + "metadata/m2-eq.avro", 1, 2, 2),
        ]
        snap3 = [
            _manifest_list_entry(cls.PREFIX + "metadata/m3.avro", 0, 3, 3),
            _manifest_list_entry(cls.PREFIX + "metadata/m2-pos.avro", 1, 2, 2),
            _manifest_list_entry(cls.PREFIX + "metadata/m2-eq.avro", 1, 2, 2),
        ]
        _write_manifest_list(table / "metadata/snap-1.avro", list_schema, 1, 1, snap1)
        _write_manifest_list(table / "metadata/snap-2.avro", list_schema, 2, 2, snap2)
        _write_manifest_list(table / "metadata/snap-3.avro", list_schema, 3, 3, snap3)

        metadata = {
            "format-version": 2,
            "table-uuid": "synthetic",
            "location": cls.PREFIX.rstrip("/"),
            "last-sequence-number": 3,
            "last-updated-ms": 1700000003000,
            "last-column-id": 3,
            "schemas": [TABLE_SCHEMA],
            "current-schema-id": 0,
            "partition-specs": [{"spec-id": 0, "fields": []}],
            "default-spec-id": 0,
            "last-partition-id": 999,
            "properties": {},
            "current-snapshot-id": 3,
            "snapshots": [
                _snapshot_metadata(1, 1, None, cls.PREFIX + "metadata/snap-1.avro", "append"),
                _snapshot_metadata(2, 2, 1, cls.PREFIX + "metadata/snap-2.avro", "overwrite"),
                _snapshot_metadata(3, 3, 2, cls.PREFIX + "metadata/snap-3.avro", "overwrite"),
            ],
            "snapshot-log": [],
            "metadata-log": [],
            "sort-orders": [{"order-id": 0, "fields": []}],
            "default-sort-order-id": 0,
            "refs": {"main": {"snapshot-id": 3, "type": "branch"}},
        }
        (table / "metadata/v1.metadata.json").write_text(json.dumps(metadata))
        plan_path = cls.tmp / "plan.json"
        plan_path.write_text(
            json.dumps(
                {
                    "metadata": "table/metadata/v1.metadata.json",
                    "uri_mapping": {cls.PREFIX: "table/"},
                    "snapshots": [1, 2, 3],
                }
            )
        )
        cls.plan = Plan.load(plan_path)
        cls.metadata = load_table_metadata(cls.plan)
        cls.plan_path = plan_path

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def test_snapshot_2_duplicate_evidence_and_same_sequence(self):
        planned = plan_snapshot(self.plan, self.metadata, 2)
        # d1 row1 gets both a position delete (duplicate evidence is only for
        # d2 row0) and an equality delete -> deleted once, two evidence rows.
        d1_row1 = next(
            r for r in planned.scan.rows
            if r.file_state.file_path == self.D1 and r.physical_row == 1
        )
        self.assertTrue(d1_row1.deleted)
        self.assertEqual(len(d1_row1.evidence), 2)
        self.assertEqual(
            sorted({e.content_name for e in d1_row1.evidence}),
            ["equality_delete", "position_delete"],
        )
        # d2 row0 has two identical position delete records: still one row,
        # two evidences.
        d2_row0 = next(
            r for r in planned.scan.rows
            if r.file_state.file_path == self.D2 and r.physical_row == 0
        )
        self.assertTrue(d2_row0.deleted)
        self.assertEqual(len(d2_row0.evidence), 2)
        self.assertEqual(
            {e.delete_file_path for e in d2_row0.evidence}, {self.POS}
        )
        # d2 row1: equality delete has the same sequence as the data file, so
        # strict < keeps it.
        d2_row1 = next(
            r for r in planned.scan.rows
            if r.file_state.file_path == self.D2 and r.physical_row == 1
        )
        self.assertFalse(d2_row1.deleted)
        self.assertEqual(len(planned.scan.visible_rows), 3)  # d1 rows 0,2 + d2 row 1
        self.assertEqual(len(planned.scan.deleted_rows), 2)  # d1 row1 + d2 row0

    def test_snapshot_3_drops_deleted_file_and_unreferenced(self):
        opened: list[str] = []

        def factory(path):
            opened.append(str(path))
            return pq.ParquetFile(path)

        planned = plan_snapshot(self.plan, self.metadata, 3, reader_factory=factory)
        names = sorted(Path(p).name for p in opened)
        self.assertEqual(names, ["d2.parquet", "del-eq.parquet", "del-pos.parquet"])
        self.assertNotIn(self.D1, planned.scan.data_files_read)
        self.assertNotIn(self.UNREF, planned.scan.data_files_read)
        # Same data file d2: row0 still deleted by position delete seq2,
        # row1 visible because equality delete is not strictly newer.
        d2_states = [s for s in planned.scan.file_states if s.file_path == self.D2]
        self.assertEqual(len(d2_states), 1)
        self.assertEqual(d2_states[0].data_sequence, 2)
        self.assertEqual(d2_states[0].file_sequence, 2)
        visible = [
            (r.file_state.file_path, r.physical_row) for r in planned.scan.visible_rows
        ]
        self.assertEqual(visible, [(self.D2, 1)])

    def test_same_file_visibility_differs_between_snapshots(self):
        snap1 = plan_snapshot(self.plan, self.metadata, 1)
        snap2 = plan_snapshot(self.plan, self.metadata, 2)
        snap3 = plan_snapshot(self.plan, self.metadata, 3)
        def visible_for(planned, path):
            return [
                r.physical_row for r in planned.scan.rows
                if r.file_state.file_path == path and not r.deleted
            ]
        self.assertEqual(visible_for(snap1, self.D1), [0, 1, 2])
        self.assertEqual(visible_for(snap2, self.D1), [0, 2])
        self.assertEqual(visible_for(snap3, self.D1), [])  # file not live / not read
        self.assertEqual(visible_for(snap2, self.D2), [1])
        self.assertEqual(visible_for(snap3, self.D2), [1])

    def test_independent_replay_matches_synthetic(self):
        for snapshot_id in (1, 2, 3):
            oracle = replay_snapshot(self.plan_path, snapshot_id)
            ours = _scanner_sets(self.plan, self.metadata, snapshot_id)
            self.assertEqual(ours["scan"], oracle["scan"], f"synthetic scan {snapshot_id}")
            self.assertEqual(ours["visible"], oracle["visible"])
            self.assertEqual(ours["deleted"], oracle["deleted"])
            # Multiset of evidence hits: repeated (d2,0) records count twice.
            def count_hits(entries, ours_style):
                result = []
                for entry in entries:
                    for hit in entry["hits"]:
                        if ours_style:
                            result.append(
                                (
                                    entry["file_path"],
                                    entry["physical_row"],
                                    hit["content"].replace("_delete", ""),
                                    hit["delete_file_path"],
                                )
                            )
                        else:
                            result.append(
                                (
                                    entry["file_path"],
                                    entry["physical_row"],
                                    hit["content"],
                                    hit["file"],
                                )
                            )
                return sorted(result, key=repr)
            self.assertEqual(
                count_hits(ours["evidence"], True),
                count_hits(oracle["evidence"], False),
                f"synthetic evidence {snapshot_id}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
