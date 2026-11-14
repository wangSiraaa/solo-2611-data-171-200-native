#!/usr/bin/env python3
"""Offline Iceberg v2 scan / explain CLI.

Usage
-----
python -m iceberg_offline scan   --plan fixtures/plan.json --snapshot 103 --out OUT
python -m iceberg_offline scan   --plan fixtures/plan.json --all-snapshots --out OUT
python -m iceberg_offline explain --plan fixtures/plan.json --snapshot 103 \
    --file s3://offline-fixture/table/data/a.parquet --row 3

All file access goes through plan.json's fixed URI mapping; no network,
catalog, or S3 calls are made.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import IcebergError
from .metadata import load_table_metadata
from .output import write_snapshot_outputs, write_visible_rows
from .plan import Plan
from .scan import explain_row, plan_snapshot


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--plan",
        required=True,
        help="path to plan.json (fixed URI -> local path mapping)",
    )
    parser.add_argument(
        "--snapshot",
        type=int,
        help="snapshot id to read (see table metadata / plan.json snapshots)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline Iceberg v2 scanner (unpartitioned, flat schema)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan_parser = sub.add_parser("scan", help="materialize rows visible in a snapshot")
    _add_common(scan_parser)
    scan_parser.add_argument(
        "--all-snapshots",
        action="store_true",
        help="scan every snapshot listed in plan.json",
    )
    scan_parser.add_argument(
        "--out",
        help="output directory (required unless --all-snapshots)",
    )
    scan_parser.add_argument(
        "--rows-only",
        action="store_true",
        help="write only rows.parquet (no files/evidence/plan sidecars)",
    )

    explain_parser = sub.add_parser(
        "explain", help="explain one physical row in a snapshot"
    )
    _add_common(explain_parser)
    explain_parser.add_argument(
        "--file", required=True, help="data file URI as it appears in the manifest"
    )
    explain_parser.add_argument(
        "--row", required=True, type=int, help="zero-based physical row in that file"
    )
    return parser


def _resolve_snapshot_ids(plan: Plan, metadata, args) -> list[int]:
    if args.all_snapshots:
        if not plan.snapshots:
            return sorted(metadata.snapshots)
        return list(plan.snapshots)
    if args.snapshot is None:
        if metadata.current_snapshot_id is not None:
            return [metadata.current_snapshot_id]
        raise IcebergError(
            "no --snapshot given and table metadata has no current-snapshot-id"
        )
    return [args.snapshot]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        plan = Plan.load(args.plan)
        metadata = load_table_metadata(plan)
    except IcebergError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "scan":
            snapshot_ids = _resolve_snapshot_ids(plan, metadata, args)
            plan.validate_snapshot_selection(snapshot_ids)
            if not args.all_snapshots and not args.out:
                raise IcebergError("scan requires --out (or use --all-snapshots)")
            results = []
            for snapshot_id in snapshot_ids:
                planned = plan_snapshot(plan, metadata, snapshot_id)
                scan = planned.scan
                if args.all_snapshots:
                    assert args.out, "output directory required for --all-snapshots"
                    write_snapshot_outputs(
                        Path(args.out) / f"snapshot-{snapshot_id}", metadata.schema, scan
                    )
                    target = str(Path(args.out) / f"snapshot-{snapshot_id}")
                else:
                    if args.rows_only:
                        path = Path(args.out)
                        if path.is_dir() or not path.suffix:
                            path = path / "rows.parquet"
                            path.parent.mkdir(parents=True, exist_ok=True)
                        write_visible_rows(path, metadata.schema, scan)
                    else:
                        write_snapshot_outputs(Path(args.out), metadata.schema, scan)
                    target = str(args.out)
                results.append(
                    {
                        "snapshot_id": snapshot_id,
                        "sequence_number": scan.sequence_number,
                        "scanned_rows": len(scan.rows),
                        "visible_rows": len(scan.visible_rows),
                        "deleted_rows": len(scan.deleted_rows),
                        "data_files_read": list(scan.data_files_read),
                        "delete_files_read": list(scan.delete_files_read),
                        "out": target,
                    }
                )
            print(
                json.dumps(
                    {"command": "scan", "results": results},
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0

        if args.command == "explain":
            if args.snapshot is None:
                raise IcebergError("explain requires --snapshot")
            plan.validate_snapshot_selection([args.snapshot])
            result = explain_row(
                plan, metadata, args.snapshot, args.file, args.row
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
    except IcebergError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
