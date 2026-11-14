"""NMO correction core.

Fixed textbook physics per README (no velocity inversion, no picking,
no denoising, no Jacobian amplitude compensation):

    v  = RMS velocity linearly interpolated in t0 (given table)
    tx = sqrt(t0**2 + (offset / v)**2)         # source time on the trace
    recorded sample times: delay_s + k*dt_s    # sample index is NOT t=0
    amplitude(tx): linear interpolation inside the recorded support
                   [delay_s, delay_s + (ns-1)*dt_s]; exact sample hits
                   permitted; never extrapolated
    stretch = tx/t0 - 1;  t0 == 0: 0 if offset == 0 else +inf;
    muted when stretch strictly greater than plan.stretch_limit
    (equality is kept).  |offset| drives the mapping; the trace keeps
    its signed offset and identity.
"""

from __future__ import annotations

import numpy as np

from .errors import NonMonotonicMappingError, VelocityTableError
from .model import (
    CorrectResult,
    CdpResult,
    Plan,
    Reason,
    TraceRecord,
    VelocityFunction,
)

# Tolerance (in sample-index units) so that a tx which lands exactly on
# the first/last recorded sample is not lost to floating-point rounding.
_SUPPORT_TOL = 1e-9


def stretch_factor(tx: float, t0: float, offset_m: float) -> float:
    """README stretch definition: tx/t0 - 1.

    t0 == 0: 0.0 when offset == 0, +inf otherwise.
    """
    if t0 == 0.0:
        return 0.0 if offset_m == 0.0 else float("inf")
    return tx / t0 - 1.0


def is_stretch_muted(stretch: float, limit: float) -> bool:
    """Strictly greater than the limit is muted; equality is kept."""
    return bool(stretch > limit)


def sample_at(
    samples: np.ndarray, dt_s: float, delay_s: float, tx: float
) -> tuple[float, int, float] | None:
    """Linear interpolation of one trace at source time tx.

    Returns (amplitude, k, frac) with amplitude =
    (1-frac)*samples[k] + frac*samples[k+1], or None when tx lies
    outside the recorded support (no extrapolation).  An exact hit on
    the last sample returns (samples[-1], ns-1, 0.0).
    """
    ns = samples.shape[0]
    r = (tx - delay_s) / dt_s
    if r < -_SUPPORT_TOL or r > (ns - 1) + _SUPPORT_TOL:
        return None
    r = min(max(r, 0.0), float(ns - 1))
    k = int(np.floor(r))
    if k >= ns - 1:
        return float(samples[ns - 1]), ns - 1, 0.0
    frac = r - k
    amp = (1.0 - frac) * samples[k] + frac * samples[k + 1]
    return float(amp), k, frac


def nmo_tx(t0: np.ndarray, offset_m: float, v: np.ndarray) -> np.ndarray:
    """NMO time mapping; offset enters squared, so sign is irrelevant
    to tx while the trace keeps its signed offset elsewhere."""
    c = offset_m / v
    return np.sqrt(t0 * t0 + c * c)


def correct_cdp(
    traces: list[TraceRecord], velfn: VelocityFunction, plan: Plan
) -> CdpResult:
    """Correct one CDP gather onto the plan's common t0 grid."""
    t0 = plan.t0_grid
    n_t0 = t0.size
    v = velfn.interpolate(t0)  # raises if grid escapes table coverage

    n_tr = len(traces)
    tx = np.empty((n_tr, n_t0), dtype=np.float64)
    for j, tr in enumerate(traces):
        tx[j] = nmo_tx(t0, tr.offset_m, v)
        if np.any(np.diff(tx[j]) <= 0.0):
            bad = int(np.argmax(np.diff(tx[j]) <= 0.0))
            raise NonMonotonicMappingError(
                f"cdp {velfn.cdp} trace file_index={tr.file_index} "
                f"(offset={tr.offset_m} m): tx is not strictly increasing "
                f"on the output grid (tx[{bad}]={tx[j][bad]:.9f} s >= "
                f"tx[{bad + 1}]={tx[j][bad + 1]:.9f} s); the velocity "
                f"function folds the mapping back"
            )

    # Stretch with the README's t0 == 0 special cases.
    stretch = np.empty_like(tx)
    zero_t0 = t0 == 0.0
    for j, tr in enumerate(traces):
        stretch[j] = np.where(
            zero_t0,
            0.0 if tr.offset_m == 0.0 else np.inf,
            tx[j] / np.where(zero_t0, 1.0, t0) - 1.0,
        )

    corrected = np.full((n_tr, n_t0), np.nan, dtype=np.float64)
    valid = np.zeros((n_tr, n_t0), dtype=bool)
    reason = np.full((n_tr, n_t0), Reason.OK, dtype=np.uint8)

    for j, tr in enumerate(traces):
        if not tr.live:
            reason[j, :] = Reason.DEAD_TRACE
            continue
        # Same rule as is_stretch_muted, vectorised (inf > limit mutes).
        muted = stretch[j] > plan.stretch_limit
        reason[j, muted] = Reason.STRETCH_MUTE
        # Sample only where not muted; out-of-support stays invalid.
        cand = ~muted
        r = (tx[j, cand] - tr.delay_s) / tr.dt_s
        in_sup = (r >= -_SUPPORT_TOL) & (r <= (tr.ns - 1) + _SUPPORT_TOL)
        idx = np.flatnonzero(cand)
        reason[j, idx[~in_sup]] = Reason.OUT_OF_SUPPORT
        ok = idx[in_sup]
        if ok.size:
            r_ok = np.clip(r[in_sup], 0.0, tr.ns - 1.0)
            k = np.floor(r_ok).astype(np.int64)
            exact_last = k >= tr.ns - 1
            # exact hit on the last sample: use s[ns-1] with weight 1
            k = np.where(exact_last, tr.ns - 1, k)
            frac = np.where(exact_last, 0.0, r_ok - k)
            k2 = np.minimum(k + 1, tr.ns - 1)
            corrected[j, ok] = (1.0 - frac) * tr.samples[k] + frac * tr.samples[k2]
            valid[j, ok] = True
            reason[j, ok] = Reason.OK

    fold = valid.sum(axis=0).astype(np.int64)
    stack_sum = np.where(valid, corrected, 0.0).sum(axis=0)
    stack = np.where(fold > 0, stack_sum / np.maximum(fold, 1), np.nan)

    return CdpResult(
        cdp=velfn.cdp,
        t0_s=t0,
        trace_indices=np.array([t.file_index for t in traces], dtype=np.int64),
        tracl=np.array([t.tracl for t in traces], dtype=np.int64),
        offsets_m=np.array([t.offset_m for t in traces], dtype=np.float64),
        delays_s=np.array([t.delay_s for t in traces], dtype=np.float64),
        live=np.array([t.live for t in traces], dtype=bool),
        corrected=corrected,
        valid=valid,
        reason=reason,
        tx_s=tx,
        stretch=stretch,
        velocity_mps=v,
        fold=fold,
        stack_sum=stack_sum,
        stack=stack,
    )


def correct(
    segy_path: str | None = None,
    velocity_path: str | None = None,
    plan: Plan | None = None,
    traces: list[TraceRecord] | None = None,
    table: dict[int, VelocityFunction] | None = None,
) -> CorrectResult:
    """Run the full pipeline: read, group by true CDP, correct, stack.

    `traces`/`table` may be injected (tests, synthetic data); otherwise
    they are read from the given paths.
    """
    from .segy_reader import read_segy
    from .velocity import load_velocity

    if plan is None:
        raise ValueError("plan is required")
    if traces is None:
        if segy_path is None:
            raise ValueError("either traces or segy_path is required")
        traces = read_segy(segy_path, plan)
    if table is None:
        if velocity_path is None:
            raise ValueError("either table or velocity_path is required")
        table = load_velocity(velocity_path)

    groups: dict[int, list[TraceRecord]] = {}
    for tr in traces:
        groups.setdefault(tr.cdp, []).append(tr)

    result = CorrectResult(plan=plan)
    for cdp in sorted(groups):
        if cdp not in table:
            raise VelocityTableError(
                f"cdp {cdp}: present in {segy_path} but missing from the "
                f"velocity table {velocity_path}; no velocity to correct with"
            )
        # Display/stacking order: |offset| then signed offset; file order
        # is meaningless and only kept as identity.
        members = sorted(
            groups[cdp], key=lambda t: (abs(t.offset_m), t.offset_m, t.file_index)
        )
        result.cdps[cdp] = correct_cdp(members, table[cdp], plan)
        result.traces_by_cdp[cdp] = members
        for tr in members:
            if not tr.live:
                result.excluded_traces.append(
                    {
                        "file_index": tr.file_index,
                        "tracl": tr.tracl,
                        "cdp": cdp,
                        "offset_m": tr.offset_m,
                        "reason": Reason.DEAD_TRACE.name,
                    }
                )
    return result
