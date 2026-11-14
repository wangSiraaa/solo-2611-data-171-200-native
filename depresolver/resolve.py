"""``resolve`` entry point: wheelhouse + requests -> lock + report."""

from __future__ import annotations

import argparse
import json
import os
import sys

from .lock import build_lock, build_report
from .solver import Solver, dedupe_conflicts
from .target import load_target_environment
from .wheels import load_wheelhouse

DEFAULT_WHEELHOUSE = "fixtures/wheelhouse"
DEFAULT_REQUESTS = "fixtures/requests.json"
DEFAULT_TARGET_ENV = "fixtures/target-env.json"
DEFAULT_OUT_DIR = "artifacts"


def _load_requests(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError("requests.json must contain a list of cases")
    for case in data:
        if "case_id" not in case or "requirements" not in case:
            raise ValueError("each case needs case_id and requirements")
    return data


def resolve_case(
    case: dict, index, environment: dict[str, str], wheelhouse_diagnostics
) -> tuple[dict, dict]:
    case_id = case["case_id"]
    requirements = list(case["requirements"])
    solver = Solver(index, environment, environment["python_full_version"])
    constraints, input_errors = solver.parse_root_requirements(requirements)
    # Wheel-level marker problems are already recorded as wheelhouse
    # diagnostics; candidates referencing unknown environment variables were
    # dropped, so the solver simply cannot select them.
    marker_diags = solver.validate_candidates()

    if input_errors:
        # Malformed root requirements and unsupported direct URLs are diagnosed
        # before solving.
        report = build_report(
            case_id,
            requirements,
            environment,
            input_errors,
            None,
            [],
            index,
            wheelhouse_diagnostics=wheelhouse_diagnostics,
            input_errors=input_errors,
        )
        report["status"] = "invalid-inputs"
        return {}, report

    solution, conflicts = solver.solve(constraints)
    conflicts = dedupe_conflicts(conflicts)
    report = build_report(
        case_id,
        requirements,
        environment,
        marker_diags,
        solution,
        conflicts,
        index,
        wheelhouse_diagnostics=wheelhouse_diagnostics,
    )
    if solution is None:
        return {}, report
    lock = build_lock(case_id, solution, environment, requirements)
    return lock, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve local wheels into lock files")
    parser.add_argument("--wheelhouse", default=DEFAULT_WHEELHOUSE)
    parser.add_argument("--requests", default=DEFAULT_REQUESTS)
    parser.add_argument("--target-env", default=DEFAULT_TARGET_ENV)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--case", default=None, help="resolve only this case_id")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero if any case is unsolvable or invalid",
    )
    args = parser.parse_args(argv)

    environment = load_target_environment(args.target_env)
    index = load_wheelhouse(args.wheelhouse)
    wheelhouse_diagnostics = list(index.diagnostics)
    cases = _load_requests(args.requests)
    if args.case:
        cases = [c for c in cases if c["case_id"] == args.case]
        if not cases:
            print(f"unknown case {args.case!r}", file=sys.stderr)
            return 2

    os.makedirs(args.out_dir, exist_ok=True)
    exit_code = 0
    for case in cases:
        case_id = case["case_id"]
        lock, report = resolve_case(
            case, index, environment, wheelhouse_diagnostics
        )
        report_path = os.path.join(args.out_dir, f"{case_id}.report.json")
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
            fh.write("\n")
        if lock:
            lock_path = os.path.join(args.out_dir, f"{case_id}.lock.json")
            with open(lock_path, "w", encoding="utf-8") as fh:
                json.dump(lock, fh, indent=2, sort_keys=True)
                fh.write("\n")
            print(f"{case_id}: SOLVABLE -> {lock_path}")
        else:
            if args.strict:
                exit_code = 1
            print(f"{case_id}: {report['status'].upper()} -> {report_path}")
            for conflict in report.get("conflicts", [])[:5]:
                print(f"  - [{conflict['kind']}] {conflict['detail']}")
            for diag in report.get("pre_solve_diagnostics", []):
                print(f"  - [{diag['kind']}] {diag['subject']}: {diag['detail']}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
