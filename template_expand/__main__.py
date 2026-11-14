"""Command-line entry point: ``python -m template_expand``."""

from __future__ import annotations

import argparse
import json
import sys

from .api import expand_records


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="template_expand",
        description="Expand a Word template (scalar placeholders + one row loop) "
                    "once per JSON record.")
    parser.add_argument("template", help="source .docx (opened read-only)")
    parser.add_argument("records", help="JSON array of records")
    parser.add_argument("output", help="directory for generated .docx files")
    parser.add_argument("--contract", default=None,
                        help="optional template-contract.json")
    parser.add_argument("--manifest", default=None,
                        help="path for the JSON manifest (default: <output>/manifest.json)")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero if any record fails to render")
    args = parser.parse_args(argv)

    result = expand_records(args.template, args.records, args.output,
                            manifest_path=args.manifest,
                            contract_path=args.contract)
    failed = [s for s in result.statuses if not s.ok]
    print(json.dumps(
        {
            "manifest": result.manifest_path,
            "template_error": result.template_error,
            "rendered": sum(1 for s in result.statuses if s.ok),
            "failed": [s.record_id for s in failed],
        }, ensure_ascii=False, indent=2))

    # A template-wide error means nothing could be produced: always non-zero.
    if result.template_error is not None:
        return 2
    # Per-record failures are isolated and reported in the manifest; strict mode
    # turns them into a non-zero exit for CI.
    if args.strict and failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
