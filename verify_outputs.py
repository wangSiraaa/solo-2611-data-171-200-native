"""Run independent verification over the fitted outputs.

Usage: python verify_outputs.py [--plan fixtures/plan.json]
                               [--fixtures fixtures] [--out outputs]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from dtifit.verify import verify_case, verify_cross_case


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent DTI output verification")
    parser.add_argument("--plan", default="fixtures/plan.json")
    parser.add_argument("--fixtures", default="fixtures")
    parser.add_argument("--out", default="outputs")
    args = parser.parse_args(argv)

    plan = json.load(open(args.plan, encoding="utf-8"))
    reports = []
    for case in plan["cases"]:
        name = os.path.splitext(case["image"].replace(".nii", ""))[0]
        report = verify_case(
            os.path.join(args.out, name),
            os.path.join(args.fixtures, case["image"]),
            os.path.join(args.fixtures, case["gradients"]),
            plan,
        )
        reports.append(report)
        flag = "OK" if report["ok"] else "FAIL"
        print(f"[verify] {name}: {flag} ({report['voxels_recomputed']} voxels recomputed)")
        for item in report["findings"]:
            print(f"         - {item}")

    cross = verify_cross_case(args.out, plan, args.fixtures)
    reports.append(cross)
    flag = "OK" if cross["ok"] else "FAIL"
    print(f"[verify] {cross['check']}: {flag} ({cross['n_voxels_matched']} matched)")
    for item in cross["findings"]:
        print(f"         - {item}")

    with open(os.path.join(args.out, "verification_report.json"), "w", encoding="utf-8") as fh:
        json.dump(reports, fh, indent=2, ensure_ascii=False)

    return 0 if all(r["ok"] for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
