"""Plan loading and overlap planning.

A plan is accepted only in its canonical form:

    axis_permutation maps *target* axes to *source* axes
    (i.e. ``target = source.transpose(axis_permutation)``).

All iteration/intersection arithmetic below is done in *target* axis order
because that is where ``target_chunks`` is expressed.  A source chunk with
source-grid coordinate ``g`` therefore covers target-coordinate intervals
``[g[p[k]]*C[p[k]], (g[p[k]]+1)*C[p[k]])`` clipped to the target shape.
"""

import json

from .errors import PlanError
from .zmetadata import grid_dims

_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1


def load_plan(path):
    try:
        with open(path, "rb") as f:
            plan = json.loads(f.read())
    except FileNotFoundError:
        raise PlanError(f"plan not found: {path}")
    except (OSError, ValueError) as e:
        raise PlanError(f"cannot parse plan {path}: {e}")
    if not isinstance(plan, dict):
        raise PlanError("plan must be a JSON object")
    return plan


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def validate_plan(plan, source_meta):
    """Validate the plan against source metadata; return a normalized plan."""
    perm = plan.get("axis_permutation")
    if (
        not isinstance(perm, list)
        or len(perm) != 3
        or sorted(perm) != [0, 1, 2]
        or not all(_is_int(v) for v in perm)
    ):
        raise PlanError(f"axis_permutation must be a permutation of [0,1,2], got {perm!r}")

    tchunks = plan.get("target_chunks")
    if (
        not isinstance(tchunks, list)
        or len(tchunks) != 3
        or not all(_is_int(v) and v > 0 for v in tchunks)
    ):
        raise PlanError(f"target_chunks must be three positive integers, got {tchunks!r}")

    budget = plan.get("data_buffer_budget_bytes")
    if not _is_int(budget) or budget <= 0:
        raise PlanError(f"data_buffer_budget_bytes must be a positive integer, got {budget!r}")

    comp = plan.get("target_compressor", {"id": "zlib", "level": 1})
    if not isinstance(comp, dict) or comp.get("id") != "zlib":
        raise PlanError(f"target_compressor must be a single zlib codec, got {comp!r}")
    level = comp.get("level", 1)
    if not _is_int(level) or not -1 <= level <= 9:
        raise PlanError(f"target_compressor level must be in [-1, 9], got {level!r}")

    # Reject an explicitly unsupported dtype larger than int32 if present.
    dtype = plan.get("target_dtype", source_meta["dtype"])
    if dtype != "<i4":
        raise PlanError(f"only target dtype <i4 is supported, got {dtype!r}")

    shape = source_meta["shape"]
    chunks = source_meta["chunks"]
    p = tuple(perm)
    target_shape = tuple(shape[p[k]] for k in range(3))

    fill = plan.get("target_fill_value", source_meta["fill_value"])
    if fill != source_meta["fill_value"]:
        raise PlanError(
            f"target_fill_value {fill!r} differs from source fill_value "
            f"{source_meta['fill_value']!r}; only a true transpose is supported"
        )

    # Intrinsic limits: a single chunk of either side must fit the budget.
    src_chunk_bytes = source_meta["chunk_bytes"]
    tgt_chunk_bytes = _product(tchunks) * 4
    if src_chunk_bytes + tgt_chunk_bytes > budget:
        raise PlanError(
            f"budget {budget} cannot hold one source chunk ({src_chunk_bytes}) "
            f"plus one target chunk ({tgt_chunk_bytes})"
        )
    if source_meta["shape_product"] > budget:
        # Not itself an error, but there must exist a feasible spool strategy.
        pass

    return {
        "axis_permutation": p,
        # inverse: source axis -> target axis
        "inverse_permutation": tuple(p.index(i) for i in range(3)),
        "target_chunks": tuple(tchunks),
        "target_shape": target_shape,
        "target_compressor_level": level,
        "data_buffer_budget_bytes": budget,
        "target_fill_value": source_meta["fill_value"],
        "source_shape": shape,
        "source_chunks": chunks,
        "source_dtype": source_meta["dtype"],
        "source_chunk_bytes": src_chunk_bytes,
        "target_chunk_bytes": tgt_chunk_bytes,
        "source_shape_product": source_meta["shape_product"],
        "source_grid_dims": grid_dims(shape, chunks),
        "target_grid_dims": grid_dims(target_shape, tchunks),
    }


def _product(values):
    n = 1
    for v in values:
        n *= v
    return n


def target_chunk_ids_covered_by_source(g, norm):
    """Target grid coordinates touched by source chunk ``g``.

    Returns lists of ``range`` objects in target-axis order.  A source chunk
    can span several target chunks per target axis (many-to-many).
    """
    p = norm["axis_permutation"]
    C = norm["source_chunks"]
    TC = norm["target_chunks"]
    T = norm["target_shape"]
    ranges = []
    for k in range(3):
        lo = g[p[k]] * C[p[k]]
        hi = min((g[p[k]] + 1) * C[p[k]], T[k])
        if hi <= lo:  # source padding outside the logical domain
            return None
        ranges.append(range(lo // TC[k], (hi - 1) // TC[k] + 1))
    return ranges


def source_chunk_ids_covering_target(G, norm):
    """Source grid coordinates whose logical extent intersects target ``G``.

    Used by the planner for reporting; the executor drives from the source
    side so each source chunk is decoded once.
    """
    q = norm["inverse_permutation"]
    C = norm["source_chunks"]
    TC = norm["target_chunks"]
    T = norm["target_shape"]
    S = norm["source_shape"]
    target_lo = [G[k] * TC[k] for k in range(3)]
    target_hi = [min((G[k] + 1) * TC[k], T[k]) for k in range(3)]
    ranges = []
    for i in range(3):
        lo = target_lo[q[i]]
        hi = target_hi[q[i]]
        ranges.append(range(lo // C[i], (hi - 1) // C[i] + 1))
    return ranges


def intersect(g, G, norm):
    """Compute the intersection of source chunk ``g`` and target chunk ``G``.

    All returned coordinates are in target-axis order.  Returns ``None`` when
    the logical extents do not overlap.

    Result fields:
      src_local_lo/hi : half-open bounds inside the full source chunk array
      tgt_local_lo    : lower bound inside the full target chunk array
      shape           : intersection shape (== hi - lo on all axes)
      global_lo/hi    : absolute target-array coordinates of the intersection
    """
    p = norm["axis_permutation"]
    C = norm["source_chunks"]
    TC = norm["target_chunks"]
    T = norm["target_shape"]

    global_lo = []
    global_hi = []
    for k in range(3):
        src_lo = g[p[k]] * C[p[k]]
        src_hi = min((g[p[k]] + 1) * C[p[k]], T[k])
        tgt_lo = G[k] * TC[k]
        tgt_hi = min((G[k] + 1) * TC[k], T[k])
        lo = max(src_lo, tgt_lo)
        hi = min(src_hi, tgt_hi)
        if hi <= lo:
            return None
        global_lo.append(lo)
        global_hi.append(hi)

    src_local_lo, src_local_hi, tgt_local_lo = [], [], []
    for k in range(3):
        src_lo = g[p[k]] * C[p[k]]
        tgt_lo = G[k] * TC[k]
        # target axis k corresponds to source axis p[k]
        src_local_lo.append(global_lo[k] - src_lo)
        src_local_hi.append(global_hi[k] - src_lo)
        tgt_local_lo.append(global_lo[k] - tgt_lo)

    shape = tuple(global_hi[k] - global_lo[k] for k in range(3))
    return {
        "src_local_lo": tuple(src_local_lo),
        "src_local_hi": tuple(src_local_hi),
        "tgt_local_lo": tuple(tgt_local_lo),
        "shape": shape,
        "global_lo": tuple(global_lo),
        "global_hi": tuple(global_hi),
        "bytes": _product(shape) * 4,
    }


def build_plan_report(norm, source_meta, present_keys, missing_keys):
    """A deterministic, JSON-serialisable description of the conversion plan.

    ``entries`` contains every logical source/target chunk pair intersection,
    with both local and global coordinates, so the mapping is auditable.
    """
    src_grid = norm["source_grid_dims"]
    entries = []
    pieces_by_target = {}
    targets_by_source = {}
    n_intersections = 0
    for g in _grid_coords(src_grid):
        ranges = target_chunk_ids_covered_by_source(g, norm)
        if ranges is None:
            targets_by_source[chunk_key(g)] = []
            continue
        touched = []
        for G in _product_coords(ranges):
            piece = intersect(g, G, norm)
            if piece is None:
                continue
            n_intersections += 1
            gk = chunk_key(g)
            Gk = chunk_key(G)
            touched.append(Gk)
            pieces_by_target.setdefault(Gk, []).append(gk)
            entries.append({
                "source_key": gk,
                "source_present": gk not in missing_keys,
                "target_key": Gk,
                "global_lo": list(piece["global_lo"]),
                "global_hi": list(piece["global_hi"]),
                "src_local_lo": list(piece["src_local_lo"]),
                "src_local_hi": list(piece["src_local_hi"]),
                "tgt_local_lo": list(piece["tgt_local_lo"]),
                "shape": list(piece["shape"]),
                "bytes": piece["bytes"],
            })
        targets_by_source[chunk_key(g)] = sorted(set(touched))

    # Per-source decode count and per-target piece count for the plan digest.
    decode_counts = {
        k: (0 if k in missing_keys else 1) for k in sorted(targets_by_source)
    }
    piece_counts = {k: len(v) for k, v in sorted(pieces_by_target.items())}

    # Spool footprint: only present source chunks write fragments.  Each
    # fragment costs a 28-byte header plus its raw int32 payload.
    fragment_header_bytes = 28
    spool_bytes = sum(
        e["bytes"] + fragment_header_bytes
        for e in entries
        if e["source_key"] not in missing_keys
    )

    return {
        "source": {
            "shape": list(source_meta["shape"]),
            "chunks": list(source_meta["chunks"]),
            "dtype": source_meta["dtype"],
            "fill_value": source_meta["fill_value"],
            "order": source_meta["order"],
            "compressor": {"id": "zlib", "level": source_meta["compressor_level"]},
            "grid_dims": list(src_grid),
            "logical_chunks": _product(src_grid),
            "present_chunks": len(present_keys),
            "missing_chunks": sorted(missing_keys),
            "chunk_inventory": _chunk_inventory(norm, "source", present_keys),
        },
        "target": {
            "shape": list(norm["target_shape"]),
            "chunks": list(norm["target_chunks"]),
            "dtype": "<i4",
            "fill_value": norm["target_fill_value"],
            "order": "C",
            "compressor": {"id": "zlib", "level": norm["target_compressor_level"]},
            "grid_dims": list(norm["target_grid_dims"]),
            "logical_chunks": _product(norm["target_grid_dims"]),
            "chunk_inventory": _chunk_inventory(norm, "target", None),
        },
        "axis_permutation": list(norm["axis_permutation"]),
        "inverse_permutation": list(norm["inverse_permutation"]),
        "data_buffer_budget_bytes": norm["data_buffer_budget_bytes"],
        "counts": {
            "intersections": n_intersections,
            "sum_intersection_bytes": sum(e["bytes"] for e in entries),
            "max_intersection_bytes": max((e["bytes"] for e in entries), default=0),
            "max_source_chunks_per_target": max(piece_counts.values(), default=0),
            "max_target_chunks_per_source": max(
                (len(v) for v in targets_by_source.values()), default=0
            ),
            "source_decode_calls_expected": len(present_keys),
            "target_encode_calls_expected": _product(norm["target_grid_dims"]),
        },
        "predicted_temporary_space": {
            "fragment_spool_bytes": spool_bytes,
            "note": "fragment headers plus raw intersection payloads; excludes staging output",
        },
        "source_decode_counts": decode_counts,
        "target_piece_counts": piece_counts,
        "entries": entries,
    }


def _grid_coords(dims):
    for i in range(dims[0]):
        for j in range(dims[1]):
            for k in range(dims[2]):
                yield (i, j, k)


def _product_coords(ranges):
    for i in ranges[0]:
        for j in ranges[1]:
            for k in ranges[2]:
                yield (i, j, k)


def _chunk_inventory(norm, side, present_keys):
    """Per-chunk physical vs. logical extent.

    Explains which chunks are *physical edge* chunks (their logical extent is
    smaller than the declared chunk shape and the remainder is padding) and,
    for the source, whether each chunk is present or missing (fill semantics).
    """
    if side == "source":
        shape = norm["source_shape"]
        chunks = norm["source_chunks"]
        dims = norm["source_grid_dims"]
    else:
        shape = norm["target_shape"]
        chunks = norm["target_chunks"]
        dims = norm["target_grid_dims"]
    inventory = {}
    for g in _grid_coords(dims):
        key = chunk_key(g)
        logical = tuple(min(chunks[i], shape[i] - g[i] * chunks[i]) for i in range(3))
        is_edge = any(logical[i] != chunks[i] for i in range(3))
        row = {
            "declared": list(chunks),
            "logical": list(logical),
            "physical": list(chunks),
            "edge": is_edge,
            "padding_cells": _product(chunks) - _product(logical),
        }
        if side == "source":
            row["present"] = key in present_keys
            row["missing_fill_semantics"] = key not in present_keys
        inventory[key] = row
    return inventory


def chunk_key(coord):
    return ".".join(str(int(v)) for v in coord)


def planned_peak_bytes(norm, plan_report):
    """Conservative estimate of the peak *numeric* data buffer footprint.

    The executor never holds a decoded source chunk and a target assembly at
    the same time, so the peak is the larger of the two stages.
    """
    src_chunk = norm["source_chunk_bytes"]
    tgt_chunk = norm["target_chunk_bytes"]
    max_piece = plan_report["counts"]["max_intersection_bytes"]
    # Scatter stage: decoded source chunk + one intersection copy.
    scatter = src_chunk + max_piece
    # Assembly stage: target chunk + one spool fragment read.
    assembly = tgt_chunk + max_piece
    # A read fragment is a bytes object decoded into a fresh ndarray; count it
    # once (the ndarray) plus the transient bytes buffer is reported
    # separately as compressed bytes.  Here we count the numeric array.
    return {
        "scatter_stage_bytes": scatter,
        "assembly_stage_bytes": assembly,
        "predicted_peak_bytes": max(scatter, assembly),
    }
