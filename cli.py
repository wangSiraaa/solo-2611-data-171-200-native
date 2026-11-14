#!/usr/bin/env python3
"""命令行: canonicalize / compare。

用法:
  python cli.py canonicalize FILE.nq [--out DIR]
  python cli.py compare A.nq B.nq [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from rdfcanon import canonicalize, compare, parse_nquads_file


def _write(out_dir: str, name: str, text: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def cmd_canonicalize(args) -> int:
    ds = parse_nquads_file(args.file)
    result = canonicalize(ds)
    print(result.canonical_nquads, end="")
    print(f"# sha256: {result.sha256}", file=sys.stderr)
    print(f"# stats: {json.dumps(result.stats, ensure_ascii=False)}", file=sys.stderr)
    if args.out:
        base = os.path.splitext(os.path.basename(args.file))[0]
        nq = _write(args.out, base + ".canonical.nq", result.canonical_nquads)
        diag = {
            "source": args.file,
            "sha256": result.sha256,
            "labeling": result.labeling,
            "stats": result.stats,
            "quads": len(ds),
            "duplicates_folded": ds.duplicate_count,
        }
        js = _write(args.out, base + ".canonical.json",
                    json.dumps(diag, ensure_ascii=False, indent=2) + "\n")
        print(f"# wrote {nq} and {js}", file=sys.stderr)
    return 0


def cmd_compare(args) -> int:
    ds_a = parse_nquads_file(args.a)
    ds_b = parse_nquads_file(args.b)
    result = compare(ds_a, ds_b)
    report = result.to_dict()
    report["file_a"] = args.a
    report["file_b"] = args.b
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    print(text, end="")
    if result.equivalent:
        print("# EQUIVALENT", file=sys.stderr)
    else:
        print("# NOT EQUIVALENT", file=sys.stderr)
    if args.out:
        na = os.path.splitext(os.path.basename(args.a))[0]
        nb = os.path.splitext(os.path.basename(args.b))[0]
        path = _write(args.out, f"compare-{na}-{nb}.json", text)
        _write(args.out, na + ".canonical.nq", result.canon_a.canonical_nquads)
        _write(args.out, nb + ".canonical.nq", result.canon_b.canonical_nquads)
        print(f"# wrote {path}", file=sys.stderr)
    return 0 if result.equivalent else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="RDF dataset canonicalize / compare")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("canonicalize")
    p1.add_argument("file")
    p1.add_argument("--out")
    p1.set_defaults(fn=cmd_canonicalize)
    p2 = sub.add_parser("compare")
    p2.add_argument("a")
    p2.add_argument("b")
    p2.add_argument("--out")
    p2.set_defaults(fn=cmd_compare)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
