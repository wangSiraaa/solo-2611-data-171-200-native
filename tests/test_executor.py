"""End-to-end tests for the executor and verifier.

The fixture corpus is used as-is; additional synthetic stores are written
with the independent ``zarr`` library so that non-divisible shapes,
permutations, missing chunks and all-fill target chunks are all exercised.
"""

import json
import os
import shutil
import zlib

import numpy as np
import pytest

from rechunk_tool import executor as executor_mod
from rechunk_tool.cli import _execute
from rechunk_tool.errors import BudgetExceeded, CorruptSource, RechunkError
from rechunk_tool.plan import build_plan_report, load_plan, validate_plan
from rechunk_tool.verifier import corruption_probe, verify
from rechunk_tool.zmetadata import all_grid_coords, chunk_key, grid_dims, load_source_meta

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
SOURCE = os.path.join(FIXTURES, "source.zarr")
PLAN = os.path.join(FIXTURES, "plan.json")
CORRUPT = os.path.join(FIXTURES, "corrupt-chunk.bin")


def _read_source_meta():
    return load_source_meta(SOURCE)


def _norm_and_report(source_root=SOURCE, plan_path=PLAN):
    meta = load_source_meta(source_root)
    norm = validate_plan(load_plan(plan_path), meta)
    present, missing = [], []
    for coord in all_grid_coords(grid_dims(meta["shape"], meta["chunks"])):
        key = chunk_key(coord)
        if os.path.exists(os.path.join(source_root, key)):
            present.append(key)
        else:
            missing.append(key)
    report = build_plan_report(norm, meta, present, missing)
    return meta, norm, report


def make_plan_file(path, perm, tchunks, budget, level=1, fill=-999):
    with open(path, "w") as f:
        json.dump({
            "axis_permutation": list(perm),
            "target_chunks": list(tchunks),
            "target_compressor": {"id": "zlib", "level": level},
            "target_fill_value": fill,
            "data_buffer_budget_bytes": budget,
        }, f)


def write_synthetic_source(path, shape, chunks, fill, missing_keys=(), order="C"):
    """Write a real Zarr v2 store with zlib chunks, then delete some keys."""
    import zarr

    arr = zarr.open(
        path, mode="w", shape=shape, chunks=chunks, dtype="<i4",
        compressor=zarr.codecs.Zlib(level=1), fill_value=fill, order=order,
    )
    rng = np.random.default_rng(2024)
    # Distinctive values: fill, zero and negative all present inside data.
    data = rng.integers(-1000, 100000, size=shape, dtype=np.int32)
    data[0, 0, 0] = 0
    data[0, 1, 1] = -12345
    data[-1, -1, -1] = 777
    data[1, 2, 0] = fill  # a value equal to fill, legitimately present
    arr[:] = data
    for key in missing_keys:
        os.remove(os.path.join(path, key))
    return data


@pytest.fixture
def workdir(tmp_path):
    return tmp_path


def test_fixture_conversion_and_verification(workdir):
    target = str(workdir / "out.zarr")
    result = _execute(SOURCE, PLAN, target, str(workdir / "work"))
    ex = result["execution"]

    assert ex["source_present_chunks"] == 32
    assert ex["source_missing_chunks"] == 4
    assert ex["source_decode_calls"] == 32
    assert ex["source_fill_chunks_zero_decode"] == 4
    assert ex["target_encode_calls"] == 64
    assert ex["target_write_calls"] == 64
    assert ex["decode_calls_per_source"] == {
        k: 0 if k in ex["missing_source_chunks"] else 1
        for k in ex["decode_calls_per_source"]
    }
    assert max(ex["encode_calls_per_target"].values()) == 1
    assert ex["counts"]["per_source_decode_max"] == 1
    assert ex["counts"]["per_target_encode_max"] == 1

    acc = ex["data_buffer_accounting"]
    assert acc["peak_bytes"] <= 12288
    assert acc["live_at_end"] == []
    assert acc["peak_stage"] in ("scatter", "assemble", "encode")
    assert ex["compressed_bytes"]["input_compressed_bytes"] > 0
    assert ex["temporary_space"]["fragment_spool_bytes"] > 0

    meta, norm, _ = _norm_and_report()
    report = verify(SOURCE, target, norm)
    assert report["full_read"]["mismatches"] == 0
    assert report["full_read"]["identical"] is True
    assert report["full_read"]["expected_sha256"] == report["full_read"]["target_sha256"]
    assert all(p["match"] for p in report["junction_probes"])
    # Real zero and real negative survive.
    assert report["value_semantics"]["target_zero_count"] == 1
    assert report["value_semantics"]["target_negative_nonfill_count"] == 1


def test_fixture_missing_chunks_become_fill(workdir):
    target = str(workdir / "out.zarr")
    _execute(SOURCE, PLAN, target, str(workdir / "work"))
    import zarr

    src = zarr.open(SOURCE, mode="r")[:]
    tgt = zarr.open(target, mode="r")[:]
    # Target 0.0.0 overlaps missing source chunk 0.0.0 across much of its
    # extent; its still-padded cells must decode as fill, not zero.
    chunk = tgt[:11, :9, :10]
    assert int((chunk == -999).sum()) > 0
    assert int((chunk == 0).sum()) == 0
    # Every target key present exactly once, and the sparse fill cells equal
    # the transpose of the fill source cells.
    names = sorted(n for n in os.listdir(target) if not n.startswith("."))
    assert len(names) == 64 and len(set(names)) == 64
    expected = src.transpose(2, 0, 1)
    assert int((tgt == -999).sum()) == int((expected == -999).sum())


def test_synthetic_non_divisible_permuted_store(workdir):
    src = str(workdir / "src.zarr")
    # 5x7x3, chunks 2x3x2: edges in every axis; remove a middle and edge chunk.
    data = write_synthetic_source(
        src, (5, 7, 3), (2, 3, 2), fill=-7,
        missing_keys=("1.1.1", "2.2.0"),
    )
    plan_path = str(workdir / "plan.json")
    make_plan_file(plan_path, (2, 0, 1), (2, 2, 3), budget=2048, fill=-7)
    target = str(workdir / "out.zarr")
    result = _execute(src, plan_path, target, str(workdir / "work"))
    ex = result["execution"]
    # Present chunks: ceil(5/2)*ceil(7/3)*ceil(3/2)=3*3*2=18, minus 2 missing.
    assert ex["source_decode_calls"] == 16
    assert ex["source_fill_chunks_zero_decode"] == 2
    assert ex["target_encode_calls"] == 3 * 3 * 2  # ceil(3/2)*ceil(5/2)*ceil(7/3)
    assert ex["data_buffer_accounting"]["peak_bytes"] <= 2048

    meta = load_source_meta(src)
    norm = validate_plan(load_plan(plan_path), meta)
    report = verify(src, target, norm)
    assert report["full_read"]["identical"]
    # Build the expected array from the in-memory data, substituting fill for
    # the two source chunks that were deleted (missing == fill semantics).
    expected_source = data.copy()
    for key in ("1.1.1", "2.2.0"):
        gi, gj, gk = (int(v) for v in key.split("."))
        sl = (
            slice(gi * 2, min((gi + 1) * 2, 5)),
            slice(gj * 3, min((gj + 1) * 3, 7)),
            slice(gk * 2, min((gk + 1) * 2, 3)),
        )
        expected_source[sl] = -7
    expected = np.ascontiguousarray(expected_source.transpose(2, 0, 1))
    import zarr
    got = zarr.open(target, mode="r")[:]
    assert got.dtype == np.dtype("<i4")
    assert np.array_equal(got, expected)
    # Missing chunks manifest as fill in the target; the genuinely-present
    # cell equal to fill contributes one more, and chunk 2.2.0 is clipped.
    assert int((got == -7).sum()) >= 9
    assert 0 in got and -12345 in got


def test_budget_exceeded_is_enforced(workdir):
    # A budget too small for one source + one target chunk is rejected up
    # front, and an artificially tiny budget fails inside the ledger.
    meta, norm, report = _norm_and_report()
    target = str(workdir / "out.zarr")
    with pytest.raises(RechunkError):
        make_plan_file(str(workdir / "tiny.json"), (2, 0, 1), (11, 9, 10), 100)
        _execute(SOURCE, str(workdir / "tiny.json"), target, str(workdir / "work"))

    # Directly clamp the ledger budget: the first decoded source chunk (7072
    # bytes) will not fit.
    spool = str(workdir / "spool")
    stage = str(workdir / "stage.zarr")
    os.makedirs(spool)
    os.makedirs(stage)
    ex = executor_mod.Executor(SOURCE, dict(norm, data_buffer_budget_bytes=4000),
                               report, spool, stage, {})
    with pytest.raises(BudgetExceeded):
        ex.run()
    assert not os.path.exists(os.path.join(stage, "0.0.0"))


def test_corrupt_existing_chunk_fails_and_publishes_nothing(workdir):
    bad_input = str(workdir / "bad.zarr")
    shutil.copytree(SOURCE, bad_input)
    shutil.copyfile(CORRUPT, os.path.join(bad_input, "0.0.1"))
    target = str(workdir / "out.zarr")

    def conversion_fn(i, o):
        return _execute(i, PLAN, o, None)

    with pytest.raises(CorruptSource):
        conversion_fn(bad_input, target)
    assert not os.path.exists(target)
    # No staging remnants.
    assert not [p for p in os.listdir(str(workdir)) if p.startswith(".rechunk-work")]


def test_truncated_zlib_is_detected_but_missing_is_not(tmp_path):
    bad_input = str(tmp_path / "bad.zarr")
    shutil.copytree(SOURCE, bad_input)
    with open(os.path.join(SOURCE, "0.0.1"), "rb") as f:
        good = f.read()
    with open(os.path.join(bad_input, "0.0.1"), "wb") as f:
        f.write(good[: len(good) // 2])
    target = str(tmp_path / "out.zarr")
    with pytest.raises(CorruptSource):
        _execute(bad_input, PLAN, target, None)
    assert not os.path.exists(target)


def test_wrong_decoded_size_fails(tmp_path):
    bad_input = str(tmp_path / "bad.zarr")
    shutil.copytree(SOURCE, bad_input)
    payload = zlib.compress(np.arange(100, dtype="<i4").tobytes(), 1)
    with open(os.path.join(bad_input, "0.0.1"), "wb") as f:
        f.write(payload)
    target = str(tmp_path / "out.zarr")
    with pytest.raises(CorruptSource):
        _execute(bad_input, PLAN, target, None)
    assert not os.path.exists(target)


def test_corruption_probe_helper(tmp_path):
    meta, norm, _ = _norm_and_report()

    def conversion_fn(i, o):
        return _execute(i, PLAN, o, None)

    result = corruption_probe(
        SOURCE, CORRUPT, "0.0.1", norm, work_root=str(tmp_path),
        conversion_fn=conversion_fn, record={},
    )
    assert result["failed_as_expected"] is True
    assert result["published_output_present"] is False
    assert "zlib" in result["failure"]


def test_plan_cli_reports_within_budget(tmp_path, capsys):
    from rechunk_tool.cli import main

    report_path = str(tmp_path / "plan.json")
    assert main(["plan", "--source", SOURCE, "--plan", PLAN, "--report", report_path]) == 0
    r = json.load(open(report_path))
    assert r["status"] == "ok"
    assert r["within_budget"] is True
    assert r["predicted_data_buffer"]["predicted_peak_bytes"] == 10592
    assert r["plan"]["axis_permutation"] == [2, 0, 1]


def test_fully_missing_source_chunk_yields_all_fill_target_chunk(tmp_path):
    # Source 4x4x4 with a single chunk 4x4x4 that is entirely absent.
    src = str(tmp_path / "src.zarr")
    write_synthetic_source(src, (4, 4, 4), (4, 4, 4), fill=-5)
    os.remove(os.path.join(src, "0.0.0"))
    plan_path = str(tmp_path / "plan.json")
    make_plan_file(plan_path, (2, 0, 1), (4, 4, 4), budget=1024, fill=-5)
    target = str(tmp_path / "out.zarr")
    result = _execute(src, plan_path, target, str(tmp_path / "work"))
    ex = result["execution"]
    assert ex["source_decode_calls"] == 0
    assert ex["source_fill_chunks_zero_decode"] == 1
    raw = open(os.path.join(target, "0.0.0"), "rb").read()
    decoded = np.frombuffer(zlib.decompress(raw), dtype="<i4")
    assert decoded.size == 64
    assert np.all(decoded == -5)  # fill, not zero


def test_empty_target_chunk_is_padded_with_fill(tmp_path):
    # Source 3x3x3, chunks 3x3x3 -> target shape 3x3x3 with target chunks
    # 2x2x2: the last target chunk on each axis is a physical edge chunk and
    # must be padded to the full 2x2x2 declared shape.
    src = str(tmp_path / "src.zarr")
    data = write_synthetic_source(src, (3, 3, 3), (3, 3, 3), fill=-1)
    plan_path = str(tmp_path / "plan.json")
    make_plan_file(plan_path, (2, 0, 1), (2, 2, 2), budget=1024, fill=-1)
    target = str(tmp_path / "out.zarr")
    _execute(src, plan_path, target, str(tmp_path / "work"))
    raw = open(os.path.join(target, "1.1.1"), "rb").read()
    decoded = np.frombuffer(zlib.decompress(raw), dtype="<i4").reshape(2, 2, 2)
    # The (2,2,2) physical chunk holds one real in-domain cell at (0,0,0),
    # which is target (2,2,2) -> source (2,2,2), the value 777; the rest are
    # padded with fill.
    assert int(decoded[0, 0, 0]) == int(data[2, 2, 2]) == 777
    assert np.all(decoded[0, 1:, :] == -1)
    assert np.all(decoded[1:, :, :] == -1)
    import zarr
    assert zarr.open(target, mode="r").shape == (3, 3, 3)


def test_attrs_permuted_and_units_preserved(tmp_path):
    src = str(tmp_path / "src.zarr")
    write_synthetic_source(src, (4, 6, 8), (2, 3, 4), fill=-3)
    with open(os.path.join(src, ".zattrs"), "w") as f:
        json.dump({
            "axis_names": ["time", "row", "column"],
            "units": "counts",
            "note": "keep me",
        }, f)
    plan_path = str(tmp_path / "plan.json")
    make_plan_file(plan_path, (2, 0, 1), (3, 2, 3), budget=1024, fill=-3)
    target = str(tmp_path / "out.zarr")
    _execute(src, plan_path, target, str(tmp_path / "work"))
    attrs = json.load(open(os.path.join(target, ".zattrs")))
    assert attrs["axis_names"] == ["column", "time", "row"]
    assert attrs["units"] == "counts"
    assert attrs["note"] == "keep me"
    assert attrs["rechunk"]["axis_permutation"] == [2, 0, 1]


def test_truncated_spool_fragment_is_detected(tmp_path):
    # Directly exercise the fragment reader: a truncated spool file must fail
    # before any target chunk is published.
    from rechunk_tool.errors import SpoolError

    src = str(tmp_path / "src.zarr")
    write_synthetic_source(src, (4, 4, 4), (2, 2, 2), fill=-2)
    plan_path = str(tmp_path / "plan.json")
    make_plan_file(plan_path, (0, 1, 2), (3, 3, 3), budget=2048, fill=-2)
    meta = load_source_meta(src)
    norm = validate_plan(load_plan(plan_path), meta)
    present, missing = [], []
    for coord in all_grid_coords(grid_dims(meta["shape"], meta["chunks"])):
        key = chunk_key(coord)
        (present if os.path.exists(os.path.join(src, key)) else missing).append(key)
    report = build_plan_report(norm, meta, present, missing)
    spool = str(tmp_path / "spool")
    stage = str(tmp_path / "stage.zarr")
    os.makedirs(spool)
    os.makedirs(stage)
    ex = executor_mod.Executor(src, norm, report, spool, stage, {})

    # Run scatter only, then truncate the spool of one target chunk.
    ex._write_metadata()
    ex._scatter()
    frag = os.path.join(spool, "fragments", "0.0.0.frag")
    with open(frag, "r+b") as f:
        f.truncate(os.path.getsize(frag) - 4)
    with pytest.raises(SpoolError):
        ex._assemble()
    assert not os.path.exists(os.path.join(stage, "0.0.0"))


def test_no_target_is_published_when_verification_data_path_fails(tmp_path):
    # A last-moment tamper of a staged chunk is caught by the executor's own
    # post-write validation and must not publish anything.
    from rechunk_tool.errors import VerificationError

    src = str(tmp_path / "src.zarr")
    write_synthetic_source(src, (3, 3, 3), (3, 3, 3), fill=-1)
    plan_path = str(tmp_path / "plan.json")
    make_plan_file(plan_path, (0, 1, 2), (2, 2, 2), budget=1024, fill=-1)
    meta = load_source_meta(src)
    norm = validate_plan(load_plan(plan_path), meta)
    present, missing = ["0.0.0"], []
    report = build_plan_report(norm, meta, present, missing)
    spool = str(tmp_path / "spool")
    stage = str(tmp_path / "stage.zarr")
    os.makedirs(spool)
    os.makedirs(stage)
    ex = executor_mod.Executor(src, norm, report, spool, stage, {})
    ex._write_metadata()
    ex._scatter()
    ex._assemble()
    # Corrupt the staged metadata before finalisation.
    with open(os.path.join(stage, ".zarray")) as f:
        m = json.load(f)
    m["shape"] = [1, 1, 1]
    with open(os.path.join(stage, ".zarray"), "w") as f:
        json.dump(m, f)
    with pytest.raises(VerificationError):
        ex._finalize_metadata()
