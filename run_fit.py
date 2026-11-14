"""Command-line entry: fit every case listed in the plan JSON.

Usage::

    python run_fit.py [--plan fixtures/plan.json] [--fixtures fixtures]
                      [--out outputs]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

from dtifit import FitResult, fit_case, write_case_outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Minimal DTI tensor fit")
    parser.add_argument("--plan", default="fixtures/plan.json")
    parser.add_argument("--fixtures", default="fixtures")
    parser.add_argument("--out", default="outputs")
    args = parser.parse_args(argv)

    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)

    summary: list[dict] = []
    for case in plan["cases"]:
        image_path = os.path.join(args.fixtures, case["image"])
        grad_path = os.path.join(args.fixtures, case["gradients"])
        case_name = os.path.splitext(case["image"].replace(".nii", ""))[0]
        print(f"[fit] {case_name}: {case['image']} + {case['gradients']}")
        result: FitResult = fit_case(image_path, grad_path, plan, case_name)
        out_dir = os.path.join(args.out, case_name)
        paths = write_case_outputs(result, out_dir)

        n_total = int(np.prod(result.spatial_shape))
        case_summary = {
            "case": case_name,
            "voxels": n_total,
            "fitted": int(result.fitted.sum()),
            "physical": int(result.physical.sum()),
            "metric_valid": int(result.metric_valid.sum()),
            "principal_direction_defined": int(result.principal_defined.sum()),
            "status_counts": {},
            "outputs": paths,
        }
        for diag in result.diagnostics:
            case_summary["status_counts"][diag["status"]] = (
                case_summary["status_counts"].get(diag["status"], 0) + 1
            )
        summary.append(case_summary)
        print(f"       status counts: {case_summary['status_counts']}")

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "fit_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    print(f"[fit] summary written to {os.path.join(args.out, 'fit_summary.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
