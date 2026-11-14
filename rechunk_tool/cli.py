"""Command line interface for the rechunk tool."""

import argparse
import json
import os
import shutil
import sys
import tempfile

from . import executor as executor_mod
from . import verifier as verifier_mod
from .errors import RechunkError
from .plan import build_plan_report, load_plan, planned_peak_bytes, validate_plan
from .zmetadata import all_grid_coords, chunk_key, grid_dims, load_attrs, load_source_meta


def _write_json(path, obj):
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
            f.write("\n")
    else:
        json.dump(obj, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")


def _split_source(source_root, source_meta):
    dims = grid_dims(source_meta["shape"], source_meta["chunks"])
    present, missing = [], []
    for coord in all_grid_coords(dims):
        key = chunk_key(coord)
        (present if os.path.exists(os.path.join(source_root, key)) else missing).append(key)
    return present, missing


def _load_everything(source, plan_path):
    source_meta = load_source_meta(source, label="source")
    attrs = load_attrs(source)
    plan = load_plan(plan_path)
    norm = validate_plan(plan, source_meta)
    present, missing = _split_source(source, source_meta)
    plan_report = build_plan_report(norm, source_meta, present, missing)
    return source_meta, attrs, plan, norm, plan_report


def cmd_plan(args):
    source_meta, attrs, plan, norm, plan_report = _load_everything(args.source, args.plan)
    predicted = planned_peak_bytes(norm, plan_report)
    report = {
        "status": "ok",
        "source": args.source,
        "plan": plan_report,
        "predicted_data_buffer": predicted,
        "budget_bytes": norm["data_buffer_budget_bytes"],
        "within_budget": predicted["predicted_peak_bytes"] <= norm["data_buffer_budget_bytes"],
        "source_attrs": attrs,
    }
    _write_json(args.report, report)
    return 0


def _prepare_dirs(target, work):
    target = os.path.abspath(target)
    if os.path.exists(target):
        raise RechunkError(f"target already exists, refusing to overwrite: {target}")
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    if work is None:
        work = tempfile.mkdtemp(prefix=".rechunk-work-", dir=parent)
    else:
        work = os.path.abspath(work)
        if os.path.exists(work):
            raise RechunkError(f"work directory already exists: {work}")
        # The work directory is removed on completion, so it must not be an
        # ancestor of the target (which would delete the published array).
        if os.path.commonpath([work, target]) == work:
            raise RechunkError(
                f"work directory {work} must not contain the target {target}"
            )
        os.makedirs(work)
    spool = os.path.join(work, "spool")
    stage = os.path.join(work, "stage.zarr")
    os.makedirs(spool)
    os.makedirs(stage)
    return target, work, spool, stage


def _publish(stage, target):
    if os.path.exists(target):
        raise RechunkError(f"target appeared before publish: {target}")
    # Same-filesystem rename gives an atomic, all-or-nothing publication.
    os.rename(stage, target)


def _execute(source, plan_path, target, work, keep_work=False):
    source_meta, attrs, plan, norm, plan_report = _load_everything(source, plan_path)
    target, work, spool, stage = _prepare_dirs(target, work)
    try:
        execution = executor_mod.run_conversion(source, norm, plan_report, spool, stage, attrs)
        _publish(stage, target)
        return {
            "status": "ok",
            "plan": plan_report,
            "execution": execution,
            "predicted_data_buffer": planned_peak_bytes(norm, plan_report),
            "budget_bytes": norm["data_buffer_budget_bytes"],
            "published_target": target,
        }
    except Exception:
        # Never leave a complete-looking array at the target path.
        shutil.rmtree(stage, ignore_errors=True)
        raise
    finally:
        # Spool fragments are temporary; staging has either been renamed away
        # (success) or removed (failure).
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)


def cmd_rechunk(args):
    result = _execute(args.source, args.plan, args.target, args.work,
                      keep_work=args.keep_work)
    _write_json(args.report, result)
    return 0


def _probe_conversion(input_root, output_root, plan_path):
    """Conversion entry point used by the corruption probe.

    Uses the same machinery as the CLI but in-process, and publishes only
    after the fully staged target has passed the executor's checks.
    """
    source_meta = load_source_meta(input_root, label="probe source")
    norm = validate_plan(load_plan(plan_path), source_meta)
    present, missing = _split_source(input_root, source_meta)
    report = build_plan_report(norm, source_meta, present, missing)
    parent = os.path.dirname(os.path.abspath(output_root))
    os.makedirs(parent, exist_ok=True)
    work = tempfile.mkdtemp(prefix=".probe-work-", dir=parent)
    spool = os.path.join(work, "spool")
    stage = os.path.join(work, "stage.zarr")
    os.makedirs(spool)
    os.makedirs(stage)
    try:
        executor_mod.run_conversion(input_root, norm, report, spool, stage,
                                    load_attrs(input_root))
        _publish(stage, output_root)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _maybe_probe(args, record, norm):
    if not getattr(args, "corruption_probe", False):
        return
    verifier_mod.corruption_probe(
        args.source,
        args.corrupt,
        args.replace_key,
        norm,
        work_root=os.path.dirname(os.path.abspath(args.target)),
        conversion_fn=lambda i, o: _probe_conversion(i, o, args.plan),
        record=record,
    )


def cmd_verify(args):
    plan = load_plan(args.plan)
    source_meta = load_source_meta(args.source, label="source")
    norm = validate_plan(plan, source_meta)
    result = verifier_mod.verify(args.source, args.target, norm, record={})
    _maybe_probe(args, result, norm)
    _write_json(args.report, result)
    return 0


def cmd_run(args):
    # Two clearly separated phases: execution is budgeted; verification's
    # full-array read is not charged to the executor budget.
    rechunk_result = _execute(args.source, args.plan, args.target, args.work,
                              keep_work=args.keep_work)
    plan = load_plan(args.plan)
    source_meta = load_source_meta(args.source, label="source")
    norm = validate_plan(plan, source_meta)
    verify_result = verifier_mod.verify(args.source, args.target, norm, record={})
    _maybe_probe(args, verify_result, norm)
    combined = {"status": "ok", "rechunk": rechunk_result, "verify": verify_result}
    _write_json(args.report, combined)
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="rechunk_tool",
        description="Transpose + rechunk a 3-D Zarr v2 int32 array within a data budget.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("plan", help="validate inputs and print the overlap plan")
    sp.add_argument("--source", required=True)
    sp.add_argument("--plan", required=True)
    sp.add_argument("--report")
    sp.set_defaults(func=cmd_plan)

    sr = sub.add_parser("rechunk", help="execute the conversion and publish the target")
    sr.add_argument("--source", required=True)
    sr.add_argument("--plan", required=True)
    sr.add_argument("--target", required=True)
    sr.add_argument("--work", help="temporary spool/staging root (same filesystem as target)")
    sr.add_argument("--keep-work", action="store_true",
                    help="keep the spool/staging work directory after completion")
    sr.add_argument("--report")
    sr.set_defaults(func=cmd_rechunk)

    sv = sub.add_parser("verify", help="independently verify a published target")
    sv.add_argument("--source", required=True)
    sv.add_argument("--target", required=True)
    sv.add_argument("--plan", required=True)
    sv.add_argument("--report")
    _add_probe_args(sv)
    sv.set_defaults(func=cmd_verify)

    sa = sub.add_parser("run", help="rechunk, then verify as a separate recorded phase")
    sa.add_argument("--source", required=True)
    sa.add_argument("--plan", required=True)
    sa.add_argument("--target", required=True)
    sa.add_argument("--work")
    sa.add_argument("--keep-work", action="store_true")
    sa.add_argument("--report")
    _add_probe_args(sa)
    sa.set_defaults(func=cmd_run)
    return p


def _add_probe_args(parser):
    parser.add_argument("--corruption-probe", action="store_true")
    parser.add_argument("--corrupt", default=None)
    parser.add_argument("--replace-key", default=None)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "corruption_probe", False) and (
        not args.corrupt or not args.replace_key
    ):
        parser.error("--corruption-probe requires --corrupt and --replace-key")
    try:
        return args.func(args)
    except Exception as e:
        err = {"status": "error", "error_type": type(e).__name__, "error": str(e)}
        logger = getattr(args, "report", None)
        try:
            _write_json(logger, err)
        except OSError:
            pass
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
