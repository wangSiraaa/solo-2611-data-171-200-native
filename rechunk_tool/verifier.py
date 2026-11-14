"""Independent verifier.

Runs *separately* from the executor.  It may materialise the small array
whole, and it uses the independent ``zarr``/``numcodecs`` consumer rather
than the executor's own codec helpers.

Checks performed:

1. full read of the target with ``zarr.open`` (independent consumer);
2. full read of the source with ``zarr.open`` and a transpose comparison,
   element-wise, including dtype and fill semantics;
3. a set of hand-chosen junction coordinates (edge/permutation boundaries,
   missing-chunk extents, the true zero and true negative) checked by
   on-demand source slicing, not just the transposed whole-array compare;
4. optional corruption probe: a disposable copy of the source with one key
   replaced by a bad payload must make conversion fail and leave no
   published output.
"""

import hashlib
import json
import os
import shutil
import tempfile

import numpy as np

from .errors import VerificationError


def _axis_boundary_points(shape, chunks):
    """Coordinates on and around source chunk boundaries, per axis.

    Yields small 3-coordinate tuples that hit: the origin, the cell just
    before each internal chunk boundary, the first cell at each boundary, and
    the final in-domain cell.  Combined across axes this exercises the
    many-to-many source/target overlap after permutation.
    """
    per_axis = []
    for i in range(3):
        vals = {0, shape[i] - 1}
        c = chunks[i]
        k = c
        while k < shape[i]:
            vals.add(k - 1)
            vals.add(k)
            k += c
        per_axis.append(sorted(v for v in vals if 0 <= v < shape[i]))

    points = []
    seen = set()

    def add(pt):
        pt = tuple(pt)
        if pt not in seen:
            seen.add(pt)
            points.append(pt)

    # Diagonals through each boundary value keep the probe set small while
    # still covering all axes and both sides of every boundary.
    n = max(len(a) for a in per_axis)
    for idx in range(n):
        pt = tuple(per_axis[i][min(idx, len(per_axis[i]) - 1)] for i in range(3))
        add(pt)
    for i, vals in enumerate(per_axis):
        for v in vals:
            pt = [shape[a] // 2 for a in range(3)]
            pt[i] = v
            add(pt)
    # The full extent corners.
    add((0, 0, 0))
    add(tuple(s - 1 for s in shape))
    return points


def _missing_chunk_points(source_root, shape, chunks):
    """One cell inside each missing source chunk (its first logical cell)."""
    import json

    dims = tuple((s + c - 1) // c for s, c in zip(shape, chunks))
    pts = []
    for i in range(dims[0]):
        for j in range(dims[1]):
            for k in range(dims[2]):
                key = f"{i}.{j}.{k}"
                if os.path.exists(os.path.join(source_root, key)):
                    continue
                pts.append(
                    (i * chunks[0], min(j * chunks[1], shape[1] - 1),
                     min(k * chunks[2], shape[2] - 1))
                )
    return pts


def _interesting_value_points(source_full, fill_value):
    """Locate a real zero and a real negative value if the data has them."""
    pts = []
    zero = np.argwhere((source_full == 0))
    if len(zero):
        pts.append(("true-zero", tuple(int(v) for v in zero[0])))
    neg = np.argwhere((source_full < 0) & (source_full != fill_value))
    if len(neg):
        pts.append(("true-negative", tuple(int(v) for v in neg[0])))
    fill = np.argwhere(source_full == fill_value)
    if len(fill):
        pts.append(("fill-value", tuple(int(v) for v in fill[0])))
    return pts


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _open_reader(path, label):
    try:
        import zarr
    except ImportError as e:  # pragma: no cover - environment guard
        raise VerificationError(f"{label}: independent zarr is unavailable: {e}")
    try:
        return zarr.open(path, mode="r")
    except Exception as e:
        raise VerificationError(f"{label}: independent zarr cannot open {path}: {e}")


def _read_attrs(path):
    try:
        with open(os.path.join(path, ".zattrs"), "rb") as f:
            attrs = json.loads(f.read())
        return attrs if isinstance(attrs, dict) else {}
    except (OSError, ValueError):
        return {}


def verify(source_root, target_root, plan, record=None):
    """Verify the published target against an independent full read."""
    report = {"status": "ok", "source": source_root, "target": target_root}

    src = _open_reader(source_root, "source")
    tgt = _open_reader(target_root, "target")

    perm = tuple(plan["axis_permutation"])
    expected_shape = tuple(src.shape[i] for i in perm)

    if tuple(tgt.shape) != expected_shape:
        raise VerificationError(
            f"target shape {tuple(tgt.shape)} != expected {expected_shape}"
        )
    if tgt.dtype != np.dtype("<i4") or tgt.dtype != src.dtype:
        raise VerificationError(f"dtype mismatch: source={src.dtype} target={tgt.dtype}")
    if tgt.chunks != tuple(plan["target_chunks"]):
        raise VerificationError(f"target chunks {tgt.chunks} != {plan['target_chunks']}")
    if tgt.fill_value != src.fill_value:
        raise VerificationError(f"fill_value mismatch: {tgt.fill_value} != {src.fill_value}")
    if str(tgt.order).upper() != "C":
        raise VerificationError(f"target order is not C: {tgt.order!r}")
    if tgt.compressor is None or tgt.compressor.get_config().get("id") != "zlib":
        raise VerificationError(f"target compressor is not zlib: {tgt.compressor!r}")
    if int(tgt.compressor.get_config().get("level", 1)) != int(
        plan["target_compressor_level"]
    ):
        raise VerificationError("target zlib level differs from the plan")

    # Attribute policy: axis_names permuted, units preserved.
    src_attrs = _read_attrs(source_root)
    tgt_attrs = _read_attrs(target_root)
    perm_axis_names = None
    if isinstance(src_attrs.get("axis_names"), list) and len(src_attrs["axis_names"]) == 3:
        perm_axis_names = [src_attrs["axis_names"][perm[k]] for k in range(3)]
        if tgt_attrs.get("axis_names") != perm_axis_names:
            raise VerificationError(
                f"target axis_names {tgt_attrs.get('axis_names')!r} != "
                f"permuted {perm_axis_names!r}"
            )
    if "units" in src_attrs and tgt_attrs.get("units") != src_attrs["units"]:
        raise VerificationError("target units attribute was not preserved")
    report["attributes"] = {
        "source_axis_names": src_attrs.get("axis_names"),
        "target_axis_names": tgt_attrs.get("axis_names"),
        "units_preserved": tgt_attrs.get("units") == src_attrs.get("units"),
    }

    src_full = src[:]
    tgt_full = tgt[:]
    expected = np.ascontiguousarray(np.transpose(src_full, perm))
    if expected.dtype != tgt_full.dtype:
        raise VerificationError("full-read dtype mismatch after transpose")

    eq = expected == tgt_full
    mismatch = int(np.count_nonzero(~eq))
    report["full_read"] = {
        "source_shape": list(src_full.shape),
        "target_shape": list(tgt_full.shape),
        "dtype": str(tgt_full.dtype),
        "elements": int(tgt_full.size),
        "mismatches": mismatch,
        "expected_sha256": hashlib.sha256(expected.tobytes()).hexdigest(),
        "target_sha256": hashlib.sha256(np.ascontiguousarray(tgt_full).tobytes()).hexdigest(),
        "identical": mismatch == 0,
    }
    if mismatch != 0:
        bad = np.argwhere(~eq)[:10]
        raise VerificationError(
            f"full transpose comparison found {mismatch} mismatching elements; "
            f"first positions (target coords): {bad.tolist()}"
        )

    # Structural sanity: every logical target chunk key exists exactly once.
    keys = set()
    for name in os.listdir(target_root):
        if not name.startswith("."):
            keys.add(name)
    expected_keys = {
        ".".join(str(v) for v in coord)
        for coord in _grid(plan["target_shape"], plan["target_chunks"])
    }
    if keys != expected_keys:
        raise VerificationError(
            f"target keys differ: missing={sorted(expected_keys - keys)}, "
            f"extra={sorted(keys - expected_keys)}"
        )

    # Junction probes via on-demand slicing of source and target.
    source_shape = tuple(src.shape)
    source_chunks = tuple(src.chunks)
    probes = []

    def run_probe(label, point):
        tpoint = tuple(point[perm[k]] for k in range(3))
        src_val = int(src_full[point])
        tgt_val = int(tgt_full[tpoint])
        on_demand_src = int(src[point[0], point[1], point[2]])
        on_demand_tgt = int(tgt[tpoint[0], tpoint[1], tpoint[2]])
        probes.append({
            "label": label,
            "source_coord": list(point),
            "target_coord": list(tpoint),
            "source_value_on_demand": on_demand_src,
            "target_value_on_demand": on_demand_tgt,
            "full_read_source_value": src_val,
            "full_read_target_value": tgt_val,
            "match": src_val == tgt_val == on_demand_src == on_demand_tgt,
        })
        if src_val != tgt_val:
            raise VerificationError(
                f"junction probe mismatch at source {point} -> target {tpoint}: "
                f"{src_val} != {tgt_val}"
            )
        if on_demand_src != src_val or on_demand_tgt != tgt_val:
            raise VerificationError(
                f"junction probe on-demand slicing disagrees at source {point}"
            )

    for point in _axis_boundary_points(source_shape, source_chunks):
        run_probe("chunk-boundary", point)
    for point in _missing_chunk_points(source_root, source_shape, source_chunks):
        run_probe("missing-chunk", point)
    for label, point in _interesting_value_points(src_full, src.fill_value):
        run_probe(label, point)
    report["junction_probes"] = probes

    # Aquifer check: fill semantics, true zero and true negative survive.
    report["value_semantics"] = {
        "fill_value": int(src.fill_value),
        "target_fill_count": int(np.count_nonzero(tgt_full == src.fill_value)),
        "target_zero_count": int(np.count_nonzero(tgt_full == 0)),
        "target_negative_nonfill_count": int(
            np.count_nonzero((tgt_full < 0) & (tgt_full != src.fill_value))
        ),
        "source_zero_count": int(np.count_nonzero(src_full == 0)),
        "source_negative_nonfill_count": int(
            np.count_nonzero((src_full < 0) & (src_full != src.fill_value))
        ),
    }
    if (tgt_full == 0).sum() != (src_full == 0).sum():
        raise VerificationError("true zero count changed through the transpose")
    src_neg = set(
        map(tuple, np.argwhere((src_full < 0) & (src_full != src.fill_value)))
    )
    tgt_neg_t = set(
        map(tuple, np.argwhere((tgt_full < 0) & (tgt_full != src.fill_value)))
    )
    src_neg_t = set(
        tuple(p[perm[k]] for k in range(3)) for p in src_neg
    )
    if tgt_neg_t != src_neg_t:
        raise VerificationError("true negative values did not survive the transpose")

    if record is not None:
        record.update(report)
    return report


def _grid(shape, chunks):
    dims = tuple((s + c - 1) // c for s, c in zip(shape, chunks))
    for i in range(dims[0]):
        for j in range(dims[1]):
            for k in range(dims[2]):
                yield (i, j, k)


def verify_against_expected(target_root, expected_root, plan, record=None):
    """Optional cross-check of the target against a materialised reference.

    ``expected_root`` is any independently produced Zarr directory with the
    already-transposed content.  Kept small on purpose.
    """
    tgt = _open_reader(target_root, "target")
    ref = _open_reader(expected_root, "expected")
    a = tgt[:]
    b = ref[:]
    if a.shape != b.shape or a.dtype != b.dtype:
        raise VerificationError("shape/dtype mismatch against expected reference")
    mism = int(np.count_nonzero(a != b))
    result = {"mismatches": mism, "identical": mism == 0}
    if record is not None:
        record["expected_reference"] = result
    if mism:
        raise VerificationError(f"{mism} mismatches against expected reference")
    return result


def corruption_probe(source_root, corrupt_payload, replace_key, plan, work_root,
                     conversion_fn, record=None):
    """Run a conversion on a disposable copied source with one bad chunk.

    ``conversion_fn(input_root, output_root)`` must run the executor and
    raise on failure.  The probe passes only if the conversion fails *and*
    the output root is not a published array.
    """
    try:
        import zarr
    except ImportError as e:  # pragma: no cover
        raise VerificationError(f"corruption probe: zarr unavailable: {e}")

    base = tempfile.mkdtemp(dir=work_root, prefix="corrupt-probe-")
    bad_input = os.path.join(base, "input.zarr")
    bad_output = os.path.join(base, "output.zarr")
    try:
        shutil.copytree(source_root, bad_input)
        target_file = os.path.join(bad_input, replace_key)
        if not os.path.exists(target_file):
            raise VerificationError(
                f"corruption probe: replacement key {replace_key!r} does not exist in copy"
            )
        shutil.copyfile(corrupt_payload, target_file)

        failure = None
        try:
            conversion_fn(bad_input, bad_output)
        except Exception as e:  # any failure is expected
            failure = f"{type(e).__name__}: {e}"
        published = _looks_published(bad_output)
        result = {
            "input": bad_input,
            "output": bad_output,
            "replace_key": replace_key,
            "failed_as_expected": failure is not None,
            "failure": failure,
            "published_output_present": published,
        }
        if record is not None:
            record["corruption_probe"] = result
        if failure is None:
            raise VerificationError(
                "corruption probe: conversion accepted a truncated source chunk"
            )
        if published:
            raise VerificationError(
                "corruption probe: a complete-looking target was published despite "
                "the corrupt input"
            )
        return result
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _looks_published(output_root):
    """True when the path looks like a successfully published Zarr array."""
    if not os.path.isdir(output_root):
        return False
    meta = os.path.join(output_root, ".zarray")
    if not os.path.isfile(meta):
        return False
    try:
        with open(meta, "rb") as f:
            m = json.loads(f.read())
    except Exception:
        return False
    if not isinstance(m, dict) or m.get("zarr_format") != 2:
        return False
    # A "complete-looking" array has every logical chunk key present.
    shape = m.get("shape")
    chunks = m.get("chunks")
    if not shape or not chunks:
        return False
    for coord in _grid(shape, chunks):
        if not os.path.isfile(os.path.join(output_root, ".".join(map(str, coord)))):
            return False
    return True
