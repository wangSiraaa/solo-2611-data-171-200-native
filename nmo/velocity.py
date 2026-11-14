"""RMS velocity table loading and validation.

The table is given, never inverted.  Per CDP the nodes must be strictly
increasing in t0 (foldback is diagnosed) and velocities strictly
positive.  Interpolation is linear in t0 with no extrapolation (see
VelocityFunction.interpolate in model.py).
"""

from __future__ import annotations

import csv

import numpy as np

from .errors import VelocityTableError
from .model import VelocityFunction


def load_velocity(path: str) -> dict[int, VelocityFunction]:
    """Load a cdp,t0_s,vrms_mps CSV into per-CDP velocity functions."""
    rows: dict[int, list[tuple[float, float]]] = {}
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        required = {"cdp", "t0_s", "vrms_mps"}
        if reader.fieldnames is None or not required <= set(reader.fieldnames):
            raise VelocityTableError(
                f"{path}: expected columns {sorted(required)}, "
                f"got {reader.fieldnames}"
            )
        for lineno, row in enumerate(reader, start=2):
            try:
                cdp = int(row["cdp"])
                t0 = float(row["t0_s"])
                v = float(row["vrms_mps"])
            except (TypeError, ValueError) as exc:
                raise VelocityTableError(
                    f"{path}:{lineno}: unparseable row {row!r} ({exc})"
                ) from exc
            rows.setdefault(cdp, []).append((t0, v))
    if not rows:
        raise VelocityTableError(f"{path}: velocity table is empty")

    table: dict[int, VelocityFunction] = {}
    for cdp, pairs in rows.items():
        pairs.sort(key=lambda p: p[0])
        t0 = np.array([p[0] for p in pairs], dtype=np.float64)
        v = np.array([p[1] for p in pairs], dtype=np.float64)
        if t0.size < 2:
            raise VelocityTableError(
                f"cdp {cdp}: need at least 2 velocity nodes for linear "
                f"interpolation, got {t0.size}"
            )
        if np.any(np.diff(t0) <= 0):
            raise VelocityTableError(
                f"cdp {cdp}: t0 nodes {t0.tolist()} fold back or repeat; "
                f"nodes must be strictly increasing"
            )
        if np.any(~np.isfinite(v)) or np.any(v <= 0):
            raise VelocityTableError(
                f"cdp {cdp}: velocities must be finite and strictly "
                f"positive, got {v.tolist()}"
            )
        table[cdp] = VelocityFunction(cdp=cdp, t0_s=t0, vrms_mps=v)
    return table
