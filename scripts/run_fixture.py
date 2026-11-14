#!/usr/bin/env python3
"""End-to-end demonstration and record for the supplied fixtures.

Produces four records under an output directory so that execution and
verification are separately run and separately recorded:

    plan.json        -- overlap plan and predicted peak buffer
    rechunk.json     -- execution report (budget, codec counts, temporary space)
    verify.json      -- independent full-read verification of the published
                        target, plus the corruption probe
    summary.json     -- compact human-readable summary

Run with the project virtualenv, e.g.::

    .venv/bin/python scripts/run_fixture.py --out .runtime/demo
"""

import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from rechunk_tool.cli import main  # noqa: E402


def main_cli(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=os.path.join(ROOT, "fixtures", "source.zarr"))
    ap.add_argument("--plan", default=os.path.join(ROOT, "fixtures", "plan.json"))
    ap.add_argument("--corrupt", default=os.path.join(ROOT, "fixtures", "corrupt-chunk.bin"))
    ap.add_argument("--replace-key", default="0.0.1")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    target = os.path.join(out, "target.zarr")
    if os.path.exists(target):
        shutil.rmtree(target)

    # Phase 1: plan (no data touched).
    assert main(["plan", "--source", args.source, "--plan", args.plan,
                 "--report", os.path.join(out, "plan.json")]) == 0

    # Phase 2: execution, budgeted.  Work directory is temporary.
    assert main(["rechunk", "--source", args.source, "--plan", args.plan,
                 "--target", target,
                 "--work", os.path.join(out, "work"),
                 "--report", os.path.join(out, "rechunk.json")]) == 0

    # Phase 3: verification, separate process semantics (separate CLI
    # invocation would be equivalent; we call it here for a single record).
    assert main(["verify", "--source", args.source, "--target", target,
                 "--plan", args.plan,
                 "--report", os.path.join(out, "verify.json"),
                 "--corruption-probe",
                 "--corrupt", args.corrupt,
                 "--replace-key", args.replace_key]) == 0

    plan = json.load(open(os.path.join(out, "plan.json")))
    rechunk = json.load(open(os.path.join(out, "rechunk.json")))
    verify = json.load(open(os.path.join(out, "verify.json")))
    acc = rechunk["execution"]["data_buffer_accounting"]
    summary = {
        "source_shape": rechunk["execution"]["source_shape"],
        "target_shape": rechunk["execution"]["target_shape"],
        "axis_permutation": rechunk["execution"]["axis_permutation"],
        "source_chunks": rechunk["execution"]["source_chunks"],
        "target_chunks": rechunk["execution"]["target_chunks"],
        "budget_bytes": rechunk["budget_bytes"],
        "predicted_peak_bytes": rechunk["predicted_data_buffer"]["predicted_peak_bytes"],
        "measured_peak_bytes": acc["peak_bytes"],
        "within_budget": acc["peak_bytes"] <= rechunk["budget_bytes"],
        "source_decode_calls": rechunk["execution"]["source_decode_calls"],
        "source_fill_chunks_zero_decode": rechunk["execution"]["source_fill_chunks_zero_decode"],
        "target_encode_calls": rechunk["execution"]["target_encode_calls"],
        "fragments_written": rechunk["execution"]["fragments_written"],
        "temporary_space": rechunk["execution"]["temporary_space"],
        "compressed_bytes": rechunk["execution"]["compressed_bytes"],
        "verify_identical": verify["full_read"]["identical"],
        "verify_sha256": verify["full_read"]["target_sha256"],
        "verify_mismatches": verify["full_read"]["mismatches"],
        "junction_probes": len(verify["junction_probes"]),
        "all_junction_probes_match": all(p["match"] for p in verify["junction_probes"]),
        "value_semantics": verify["value_semantics"],
        "corruption_probe_failed": verify["corruption_probe"]["failed_as_expected"],
        "corruption_probe_published": verify["corruption_probe"]["published_output_present"],
    }
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2, sort_keys=True)
        f.write("\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main_cli())
