"""Command line entry points: ``run`` and ``inspect``.

Each ``run`` invocation starts its own fresh kernel and writes an output
notebook plus a structured JSON execution record. Running the command
multiple times (or passing several notebooks) therefore produces fully
independent executions.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import nbformat

from .inspect import render_messages, render_notebook_cells, render_record
from .parameters import ParameterOverrideError
from .runner import run_notebook


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nbexec",
        description="Local notebook executor built on jupyter_client/nbformat (no nbclient wrapper).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="execute a notebook with a fresh kernel")
    run_p.add_argument("--notebook", required=True, help="source .ipynb (never modified)")
    run_p.add_argument("--output", required=True, help="path for the executed .ipynb copy")
    run_p.add_argument("--record", required=True, help="path for the structured JSON run record")
    run_p.add_argument("--parameters", help="JSON file with scalar parameter overrides")
    run_p.add_argument("--plan", help="JSON plan file; cell_timeout_seconds is read from it if present")
    run_p.add_argument(
        "--working-directory",
        default=".",
        help="cwd for the kernel; fixtures use the repository root (default: current directory)",
    )
    run_p.add_argument("--cell-timeout", type=float, default=None, help="per-cell timeout seconds")
    run_p.add_argument(
        "--interrupt-wait",
        type=float,
        default=10.0,
        help="bounded wait after interrupt / during shutdown (seconds)",
    )

    inspect_p = sub.add_parser("inspect", help="render a run record or an output notebook")
    inspect_p.add_argument("--record", help="run record JSON produced by 'run'")
    inspect_p.add_argument("--notebook", help="output notebook to inspect")
    inspect_p.add_argument("--messages", action="store_true", help="also dump the full message log")
    return parser


def _resolve_timeout(cli_timeout: Optional[float], plan: Optional[Dict[str, Any]], default: float) -> float:
    if cli_timeout is not None:
        return cli_timeout
    if plan and "cell_timeout_seconds" in plan:
        return float(plan["cell_timeout_seconds"])
    return default


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        plan = _load_json(args.plan) if args.plan else None
        parameters = _load_json(args.parameters) if args.parameters else None
        timeout = _resolve_timeout(args.cell_timeout, plan, default=30.0)

        output_path = Path(args.output)
        record_path = Path(args.record)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            result = run_notebook(
                source_path=args.notebook,
                output_path=str(output_path),
                parameters=parameters,
                working_directory=args.working_directory,
                cell_timeout=timeout,
                interrupt_wait=args.interrupt_wait,
                plan=plan,
                parameters_path=args.parameters,
            )
        except ParameterOverrideError as exc:
            print(f"nbexec: parameter error: {exc}", file=sys.stderr)
            return 2

        with open(record_path, "w", encoding="utf-8") as handle:
            json.dump(result.record, handle, indent=2, ensure_ascii=False)
            handle.write("\n")

        print(render_record(result.record))
        print(f"\nwrote notebook: {output_path}")
        print(f"wrote record:   {record_path}")
        return 0

    # inspect
    if not args.record and not args.notebook:
        parser.error("inspect requires at least --record or --notebook")
    if args.record:
        record = _load_json(args.record)
        print(render_record(record))
        if args.messages:
            print("\n--- message log ---")
            print(render_messages(record))
    if args.notebook:
        notebook = nbformat.read(args.notebook, as_version=nbformat.NO_CONVERT)
        if args.record:
            print("\n--- output notebook ---")
        print(render_notebook_cells(notebook))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
