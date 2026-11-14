"""Sample explanation entry point.

For one output sample (cdp, trace, t0 index) recompute the full chain
from first principles -- velocity, tx, stretch, mute decision, source
sample indices and interpolation weights -- and cross-check it against
the stored matrices, so a reviewer can audit exactly where a number in
the corrected gather / stack came from.
"""

from __future__ import annotations

import numpy as np

from .core import nmo_tx, sample_at, stretch_factor, is_stretch_muted
from .model import CorrectResult, Reason


def explain_sample(
    result: CorrectResult,
    cdp: int,
    file_index: int,
    t0_index: int,
) -> dict:
    """Explain one output sample.  Returns a plain dict (JSON-able)."""
    if cdp not in result.cdps:
        raise KeyError(f"cdp {cdp} not in result (have {result.cdp_ids()})")
    res = result.cdps[cdp]
    traces = result.traces_by_cdp[cdp]
    rows = {t.file_index: j for j, t in enumerate(traces)}
    if file_index not in rows:
        raise KeyError(
            f"file_index {file_index} not in cdp {cdp} "
            f"(have {sorted(rows)})"
        )
    j = rows[file_index]
    tr = traces[j]
    if not 0 <= t0_index < res.t0_s.size:
        raise IndexError(
            f"t0_index {t0_index} outside [0, {res.t0_s.size})"
        )

    t0 = float(res.t0_s[t0_index])
    # recompute from first principles (not copied from the matrices)
    v = float(res.velocity_mps[t0_index])
    tx = float(nmo_tx(np.array([t0]), tr.offset_m, np.array([v]))[0])
    stretch = stretch_factor(tx, t0, tr.offset_m)
    muted = is_stretch_muted(stretch, result.plan.stretch_limit)

    info: dict = {
        "cdp": cdp,
        "file_index": tr.file_index,
        "tracl": tr.tracl,
        "offset_m": tr.offset_m,
        "live": tr.live,
        "delay_s": tr.delay_s,
        "dt_s": tr.dt_s,
        "ns": tr.ns,
        "t0_index": t0_index,
        "t0_s": t0,
        "velocity_mps": v,
        "tx_s": tx,
        "stretch": stretch,
        "stretch_limit": result.plan.stretch_limit,
        "stretch_muted": muted,
        "support_s": [tr.time_first_s, tr.time_last_s],
    }

    if not tr.live:
        reason = Reason.DEAD_TRACE
    elif muted:
        reason = Reason.STRETCH_MUTE
    else:
        hit = sample_at(tr.samples, tr.dt_s, tr.delay_s, tx)
        if hit is None:
            reason = Reason.OUT_OF_SUPPORT
        else:
            amp, k, frac = hit
            reason = Reason.OK
            info["source"] = {
                "fractional_index": (tx - tr.delay_s) / tr.dt_s,
                "k": k,
                "k1": min(k + 1, tr.ns - 1),
                "weight_k": 1.0 - frac,
                "weight_k1": frac,
                "t_k_s": tr.delay_s + k * tr.dt_s,
                "t_k1_s": tr.delay_s + min(k + 1, tr.ns - 1) * tr.dt_s,
                "sample_k": float(tr.samples[k]),
                "sample_k1": float(tr.samples[min(k + 1, tr.ns - 1)]),
                "amplitude": amp,
            }
    info["reason"] = Reason(reason).name
    info["valid"] = reason == Reason.OK

    # cross-check against the stored matrices
    stored_reason = Reason(int(res.reason[j, t0_index]))
    assert stored_reason == reason, (
        f"explain recomputation disagrees with stored matrix: "
        f"{reason.name} vs {stored_reason.name}"
    )
    if reason == Reason.OK:
        stored_amp = float(res.corrected[j, t0_index])
        assert np.isclose(stored_amp, info["source"]["amplitude"], rtol=1e-12, atol=1e-15), (
            f"amplitude mismatch: stored {stored_amp} vs recomputed "
            f"{info['source']['amplitude']}"
        )
        info["amplitude"] = stored_amp
    info["fold_at_t0"] = int(res.fold[t0_index])
    info["stack_at_t0"] = (
        float(res.stack[t0_index]) if res.fold[t0_index] > 0 else None
    )
    return info


def format_explanation(info: dict) -> str:
    """Human-readable rendering of explain_sample's dict."""
    lines = [
        f"cdp {info['cdp']}  trace file_index={info['file_index']} "
        f"(tracl={info['tracl']}, offset={info['offset_m']:+.0f} m, "
        f"delay={info['delay_s'] * 1e3:.1f} ms, dt={info['dt_s'] * 1e6:.0f} us, "
        f"ns={info['ns']}, live={info['live']})",
        f"output t0[{info['t0_index']}] = {info['t0_s']:.6f} s",
        f"  v(t0)            = {info['velocity_mps']:.1f} m/s",
        f"  tx               = sqrt(t0^2 + (offset/v)^2) = {info['tx_s']:.9f} s",
        f"  stretch          = tx/t0 - 1 = {info['stretch']:.6f} "
        f"(limit {info['stretch_limit']}, muted={info['stretch_muted']})",
        f"  recorded support = [{info['support_s'][0]:.6f}, "
        f"{info['support_s'][1]:.6f}] s",
        f"  decision         = {info['reason']}",
    ]
    src = info.get("source")
    if src:
        lines += [
            f"  source index r   = (tx - delay)/dt = {src['fractional_index']:.6f}",
            f"  interpolate      = {src['weight_k']:.6f} * s[{src['k']}]"
            f" (t={src['t_k_s']:.6f} s, amp={src['sample_k']:+.6f})"
            f" + {src['weight_k1']:.6f} * s[{src['k1']}]"
            f" (t={src['t_k1_s']:.6f} s, amp={src['sample_k1']:+.6f})",
            f"  amplitude        = {info['amplitude']:+.6f}",
        ]
    lines.append(
        f"  fold(t0) = {info['fold_at_t0']}, stack(t0) = "
        + (f"{info['stack_at_t0']:+.6f}" if info["stack_at_t0"] is not None else "MISSING (fold=0)")
    )
    return "\n".join(lines)
