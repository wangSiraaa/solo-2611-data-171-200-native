"""Serialise correction results to NPZ + CSV (no SEG-Y re-encoding)."""

from __future__ import annotations

import csv
import json
import os

import numpy as np

from .model import REASON_NAMES, CorrectResult, Reason


def export_npz(result: CorrectResult, path: str) -> None:
    """All matrices/masks/reasons per CDP in one NPZ."""
    arrays: dict[str, np.ndarray] = {
        "t0_s": result.plan.t0_grid,
        "cdp_ids": np.array(result.cdp_ids(), dtype=np.int64),
        "plan": np.array(
            [
                result.plan.t0_start_s,
                result.plan.t0_step_s,
                result.plan.output_count,
                result.plan.stretch_limit,
                result.plan.live_trace_code,
            ]
        ),
    }
    for cdp, res in result.cdps.items():
        p = f"cdp{cdp}_"
        arrays[p + "trace_indices"] = res.trace_indices
        arrays[p + "tracl"] = res.tracl
        arrays[p + "offsets_m"] = res.offsets_m
        arrays[p + "delays_s"] = res.delays_s
        arrays[p + "live"] = res.live
        arrays[p + "corrected"] = res.corrected
        arrays[p + "valid"] = res.valid
        arrays[p + "reason"] = res.reason
        arrays[p + "tx_s"] = res.tx_s
        arrays[p + "stretch"] = res.stretch
        arrays[p + "velocity_mps"] = res.velocity_mps
        arrays[p + "fold"] = res.fold
        arrays[p + "stack_sum"] = res.stack_sum
        arrays[p + "stack"] = res.stack
    np.savez(path, **arrays)


def export_stacks_csv(result: CorrectResult, path: str) -> None:
    """Long-format per-t0 fold/sum/mean for every CDP; mean empty when
    fold == 0 (missing, not zero)."""
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["cdp", "t0_s", "fold", "stack_sum", "stack_mean"])
        for cdp in result.cdp_ids():
            res = result.cdps[cdp]
            for i, t0 in enumerate(res.t0_s):
                mean = f"{res.stack[i]:.10g}" if res.fold[i] > 0 else ""
                w.writerow(
                    [cdp, f"{t0:.6f}", int(res.fold[i]),
                     f"{res.stack_sum[i]:.10g}", mean]
                )


def export_traces_csv(result: CorrectResult, path: str) -> None:
    """Trace registry: identity, grouping, and per-reason sample counts."""
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "cdp", "file_index", "tracl", "offset_m", "delay_s", "live",
                "n_ok", "n_dead", "n_stretch_mute", "n_out_of_support",
            ]
        )
        for cdp in result.cdp_ids():
            res = result.cdps[cdp]
            for j in range(res.trace_indices.size):
                reasons = res.reason[j]
                w.writerow(
                    [
                        cdp,
                        int(res.trace_indices[j]),
                        int(res.tracl[j]),
                        f"{res.offsets_m[j]:.1f}",
                        f"{res.delays_s[j]:.6f}",
                        bool(res.live[j]),
                        int(np.count_nonzero(reasons == Reason.OK)),
                        int(np.count_nonzero(reasons == Reason.DEAD_TRACE)),
                        int(np.count_nonzero(reasons == Reason.STRETCH_MUTE)),
                        int(np.count_nonzero(reasons == Reason.OUT_OF_SUPPORT)),
                    ]
                )


def export_corrected_csvs(result: CorrectResult, outdir: str) -> list[str]:
    """One CSV per CDP: rows = traces (with identity columns), columns =
    t0 samples; invalid cells empty.  A companion *_reason.csv carries
    the per-sample reason names."""
    paths = []
    for cdp in result.cdp_ids():
        res = result.cdps[cdp]
        base = os.path.join(outdir, f"corrected_cdp{cdp}")
        header = ["file_index", "tracl", "offset_m"] + [
            f"{t:.6f}" for t in res.t0_s
        ]
        with open(base + ".csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            for j in range(res.trace_indices.size):
                row = [
                    int(res.trace_indices[j]),
                    int(res.tracl[j]),
                    f"{res.offsets_m[j]:.1f}",
                ]
                row += [
                    f"{res.corrected[j, i]:.10g}" if res.valid[j, i] else ""
                    for i in range(res.t0_s.size)
                ]
                w.writerow(row)
        with open(base + "_reason.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            for j in range(res.trace_indices.size):
                row = [
                    int(res.trace_indices[j]),
                    int(res.tracl[j]),
                    f"{res.offsets_m[j]:.1f}",
                ]
                row += [REASON_NAMES[int(res.reason[j, i])]
                        for i in range(res.t0_s.size)]
                w.writerow(row)
        paths += [base + ".csv", base + "_reason.csv"]
    return paths


def export_all(result: CorrectResult, outdir: str) -> dict[str, list[str]]:
    os.makedirs(outdir, exist_ok=True)
    written: dict[str, list[str]] = {"npz": [], "csv": [], "json": []}
    npz = os.path.join(outdir, "nmo_result.npz")
    export_npz(result, npz)
    written["npz"].append(npz)
    for name, fn in [("stacks.csv", export_stacks_csv),
                     ("traces.csv", export_traces_csv)]:
        p = os.path.join(outdir, name)
        fn(result, p)
        written["csv"].append(p)
    written["csv"] += export_corrected_csvs(result, outdir)
    meta = {
        "plan": {
            "output_t0_start_s": result.plan.t0_start_s,
            "output_t0_step_s": result.plan.t0_step_s,
            "output_count": result.plan.output_count,
            "stretch_limit": result.plan.stretch_limit,
            "live_trace_code": result.plan.live_trace_code,
        },
        "cdps": result.cdp_ids(),
        "excluded_traces": result.excluded_traces,
        "reason_codes": {r.name: int(r) for r in Reason},
    }
    p = os.path.join(outdir, "run_meta.json")
    with open(p, "w") as fh:
        json.dump(meta, fh, indent=2)
    written["json"].append(p)
    return written
