"""Unit tests for grid/intersection planning and metadata validation.

These tests exercise the arithmetic directly on the supplied fixture and on
synthetic edge-case shapes (non-divisible in all three axes, permuted and
unpermuted plans), without running a conversion.
"""

import json
import os
import shutil
import tempfile

import pytest

from rechunk_tool.errors import MetadataError, PlanError
from rechunk_tool.plan import (
    build_plan_report,
    intersect,
    load_plan,
    planned_peak_bytes,
    validate_plan,
)
from rechunk_tool.zmetadata import (
    all_grid_coords,
    chunk_key,
    chunk_local_shape,
    grid_dims,
    load_source_meta,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
SOURCE = os.path.join(FIXTURES, "source.zarr")
PLAN = os.path.join(FIXTURES, "plan.json")


def _present_missing(source_root, meta):
    dims = grid_dims(meta["shape"], meta["chunks"])
    present, missing = [], []
    for coord in all_grid_coords(dims):
        key = chunk_key(coord)
        if os.path.exists(os.path.join(source_root, key)):
            present.append(key)
        else:
            missing.append(key)
    return present, missing


@pytest.fixture
def loaded():
    meta = load_source_meta(SOURCE)
    norm = validate_plan(load_plan(PLAN), meta)
    present, missing = _present_missing(SOURCE, meta)
    report = build_plan_report(norm, meta, present, missing)
    return meta, norm, report


def make_meta(shape, chunks, fill=-999, dtype="<i4", order="C", level=1,
              filters=None):
    return {
        "shape": tuple(shape),
        "chunks": tuple(chunks),
        "dtype": dtype,
        "itemsize": 4,
        "order": order,
        "compressor_level": level,
        "fill_value": fill,
        "chunk_bytes": chunks[0] * chunks[1] * chunks[2] * 4,
        "shape_product": shape[0] * shape[1] * shape[2] * 4,
    }


def make_plan(shape, chunks, perm, tchunks, budget=12288, level=1, fill=-999):
    return {
        "axis_permutation": list(perm),
        "target_chunks": list(tchunks),
        "data_buffer_budget_bytes": budget,
        "target_compressor": {"id": "zlib", "level": level},
        "target_fill_value": fill,
    }


def test_fixture_plan_counts(loaded):
    meta, norm, report = loaded
    assert norm["source_grid_dims"] == (4, 3, 3)
    assert norm["target_grid_dims"] == (4, 4, 4)
    assert report["counts"]["intersections"] == 252
    assert report["counts"]["max_source_chunks_per_target"] == 8
    assert report["counts"]["max_target_chunks_per_source"] == 12
    assert report["counts"]["max_intersection_bytes"] == 3520
    assert sorted(report["source"]["missing_chunks"]) == [
        "0.0.0", "1.2.0", "2.1.1", "3.2.2"
    ]


def test_fixture_peak_within_budget(loaded):
    meta, norm, report = loaded
    predicted = planned_peak_bytes(norm, report)["predicted_peak_bytes"]
    assert predicted == 10592
    assert predicted <= norm["data_buffer_budget_bytes"]


def test_intersections_cover_each_logical_cell_once(loaded):
    meta, norm, report = loaded
    total = sum(e["bytes"] for e in report["entries"])
    assert total == norm["source_shape_product"]
    assert total == 29 * 37 * 41 * 4


def test_intersections_are_disjoint_and_within_chunks(loaded):
    meta, norm, report = loaded
    seen = {}
    for e in report["entries"]:
        C = norm["source_chunks"]
        TC = norm["target_chunks"]
        for k in range(3):
            assert 0 <= e["src_local_lo"][k] < C[norm["axis_permutation"][k]]
            assert 0 < e["src_local_hi"][k] <= C[norm["axis_permutation"][k]]
            assert 0 <= e["tgt_local_lo"][k] < TC[k]
            assert e["tgt_local_lo"][k] + e["shape"][k] <= TC[k]
        for coord in _cells(e):
            assert coord not in seen, (coord, e)
            seen[coord] = e["source_key"]
    assert len(seen) == 29 * 37 * 41


def _cells(entry):
    lo = entry["global_lo"]
    hi = entry["global_hi"]
    for i in range(lo[0], hi[0]):
        for j in range(lo[1], hi[1]):
            for k in range(lo[2], hi[2]):
                yield (i, j, k)


def test_missing_source_chunks_marked_absent_in_plan(loaded):
    meta, norm, report = loaded
    missing = set(report["source"]["missing_chunks"])
    for e in report["entries"]:
        assert e["source_present"] == (e["source_key"] not in missing)
    for key in missing:
        assert report["source_decode_counts"][key] == 0
    # Logically the missing chunks are still planned (they own target cells),
    # they simply contribute no decoded data.
    assert any(e["source_key"] in missing for e in report["entries"])


def test_non_divisible_all_axes_with_permutation():
    # 5x7x3 source, chunks 2x3x2 (edges on every axis) permuted (2, 0, 1).
    shape = (5, 7, 3)
    chunks = (2, 3, 2)
    meta = make_meta(shape, chunks, fill=-7)
    perm = (2, 0, 1)
    norm = validate_plan(
        make_plan(shape, chunks, perm, (2, 2, 3), budget=4096, fill=-7), meta
    )
    target_shape = tuple(shape[p] for p in perm)
    assert target_shape == (3, 5, 7)
    present, missing = _present_missing_mock(norm)
    report = build_plan_report(norm, meta, present, missing)
    # Whole array is accounted for exactly once.
    assert sum(e["bytes"] for e in report["entries"]) == 5 * 7 * 3 * 4
    # Source/target edge pieces stay inside their declared chunk bounds.
    for e in report["entries"]:
        for k in range(3):
            assert e["shape"][k] + e["tgt_local_lo"][k] <= norm["target_chunks"][k]
            assert e["shape"][k] + e["src_local_lo"][k] <= chunks[perm[k]]


def _present_missing_mock(norm):
    present = [chunk_key(c) for c in all_grid_coords(norm["source_grid_dims"])]
    return present, []


def test_identity_permutation_is_not_shape_change():
    # Changing chunk shape only (permutation (0,1,2)) still maps local coordinates.
    meta = make_meta((10, 4, 6), (3, 2, 2))
    norm = validate_plan(
        make_plan((10, 4, 6), (3, 2, 2), (0, 1, 2), (4, 3, 4)), meta
    )
    # Source chunk (1,0,0) covers [3,6)x[0,2)x[0,2); target chunk (1,0,0)
    # covers [4,8)x[0,3)x[0,4); the intersection starts at global 4.
    piece = intersect((1, 0, 0), (1, 0, 0), norm)
    assert piece is not None
    assert piece["global_lo"] == (4, 0, 0)
    assert piece["src_local_lo"] == (1, 0, 0)
    assert piece["tgt_local_lo"] == (0, 0, 0)
    assert piece["shape"] == (2, 2, 2)


def test_axis_permutation_is_distinguished_from_shape_only():
    # With perm (2,0,1) the target's axis 0 comes from source axis 2.
    meta = make_meta((4, 5, 6), (2, 3, 2))
    norm = validate_plan(
        make_plan((4, 5, 6), (2, 3, 2), (2, 0, 1), (3, 2, 2)), meta
    )
    # target shape = (6, 4, 5); target chunk (2,1,0) covers
    # [6,9)x[2,4)x[0,2) which clips to [6,6) -> empty, so pick (1,1,0):
    # [3,6)x[2,4)x[0,2).
    G = (1, 1, 0)
    # Source chunk covering target col 3..5 is source chunk index 1 on axis 2;
    # target time 2..3 -> source axis 0 chunk 1; target row 0..1 -> source
    # axis 1 chunk 0.  So source grid coord is (1, 0, 1).
    piece = intersect((1, 0, 1), G, norm)
    assert piece is not None
    src_lo = (1 * 2, 0 * 3, 1 * 2)  # (time, row, col)
    # target global lower bound must be the source coords permuted.
    assert tuple(piece["global_lo"][k] for k in range(3)) == (
        src_lo[2] + 1,  # col starts at 3 but target chunk starts at 3 -> 3
        src_lo[0],
        src_lo[1],
    )
    assert piece["shape"] == (1, 2, 2)


def test_reject_bad_metadata(tmp_path):
    base = json.load(open(os.path.join(SOURCE, ".zarray"), "rb"))
    bad = tmp_path / "bad.zarr"
    bad.mkdir()
    for mutate, err in [
        (lambda m: m.update(order="F"), MetadataError),
        (lambda m: m.update(dtype=">i4"), MetadataError),
        (lambda m: m.update(dtype="<f4"), MetadataError),
        (lambda m: m.update(filters=["delta"]), MetadataError),
        (lambda m: m.update(compressor={"id": "blosc", "cname": "lz4"}), MetadataError),
        (lambda m: m.update(shape=[29, 37]), MetadataError),
        (lambda m: m.update(zarr_format=3), MetadataError),
        (lambda m: m.update(fill_value=None), MetadataError),
        (lambda m: m.update(dimension_separator="/"), MetadataError),
    ]:
        m = dict(base)
        mutate(m)
        (bad / ".zarray").write_text(json.dumps(m))
        with pytest.raises(err):
            load_source_meta(str(bad))


def test_reject_bad_plan():
    meta = make_meta((10, 10, 10), (2, 2, 2))
    with pytest.raises(PlanError):
        validate_plan(make_plan((10, 10, 10), (2, 2, 2), (0, 0, 2), (5, 5, 5)), meta)
    with pytest.raises(PlanError):
        validate_plan(make_plan((10, 10, 10), (2, 2, 2), (0, 1, 2), (-1, 5, 5)), meta)
    with pytest.raises(PlanError):
        validate_plan(
            make_plan((10, 10, 10), (2, 2, 2), (0, 1, 2), (5, 5, 5), fill=-1), meta
        )
    with pytest.raises(PlanError):
        validate_plan(
            make_plan((10, 10, 10), (2, 2, 2), (0, 1, 2), (5, 5, 5), budget=100),
            meta,
        )


def test_chunk_local_shape_clipping():
    meta = make_meta((29, 37, 41), (8, 13, 17))
    assert chunk_local_shape((3, 2, 2), meta["shape"], meta["chunks"]) == (5, 11, 7)
    assert chunk_local_shape((0, 0, 0), meta["shape"], meta["chunks"]) == (8, 13, 17)
