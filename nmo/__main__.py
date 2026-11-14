"""Command line interface.

    python -m nmo run GATHERS.sgy VELOCITY.csv PLAN.json [--out DIR]
    python -m nmo explain GATHERS.sgy VELOCITY.csv PLAN.json \
        --cdp 101 --trace 7 --t0-index 200
"""

from __future__ import annotations

import argparse
import json
import sys

from .core import correct
from .errors import NmoError
from .explain import explain_sample, format_explanation
from .export import export_all
from .plan import load_plan


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m nmo",
        description="Textbook NMO correction + stack for small CMP gathers",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "explain"):
        sp = sub.add_parser(name)
        sp.add_argument("segy", help="input SEG-Y (big endian, format 5)")
        sp.add_argument("velocity", help="velocity CSV: cdp,t0_s,vrms_mps")
        sp.add_argument("plan", help="plan JSON")
        if name == "run":
            sp.add_argument("--out", default="nmo_output",
                            help="output directory (default: nmo_output)")
            sp.add_argument("--explain", nargs=3, type=int,
                            metavar=("CDP", "FILE_INDEX", "T0_INDEX"),
                            help="also explain one output sample")
        else:
            sp.add_argument("--cdp", type=int, required=True)
            sp.add_argument("--trace", type=int, required=True,
                            help="file_index of the trace (0-based file order)")
            sp.add_argument("--t0-index", type=int, required=True)
            sp.add_argument("--json", action="store_true",
                            help="print raw JSON instead of text")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        plan = load_plan(args.plan)
        result = correct(args.segy, args.velocity, plan)
        if args.cmd == "run":
            written = export_all(result, args.out)
            for cdp in result.cdp_ids():
                res = result.cdps[cdp]
                live = int(res.live.sum())
                print(
                    f"cdp {cdp}: {res.trace_indices.size} traces "
                    f"({live} live), fold max {int(res.fold.max())}, "
                    f"live t0 samples {int((res.fold > 0).sum())}/"
                    f"{res.t0_s.size}"
                )
            if result.excluded_traces:
                print("excluded (dead) traces:", result.excluded_traces)
            for kind, paths in written.items():
                for p in paths:
                    print(f"wrote {kind}: {p}")
            if args.explain:
                cdp, fi, ti = args.explain
                print()
                print(format_explanation(explain_sample(result, cdp, fi, ti)))
        else:
            info = explain_sample(result, args.cdp, args.trace, args.t0_index)
            if args.json:
                print(json.dumps(info, indent=2))
            else:
                print(format_explanation(info))
    except NmoError as exc:
        print(f"DIAGNOSTIC: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
