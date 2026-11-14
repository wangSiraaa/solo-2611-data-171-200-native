"""Command line entries: rebuild, serve, verify."""

from __future__ import annotations

import argparse
import json
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="siterebuild",
        description="Offline rebuild of an archived site from WARC + cutoff plan",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_rebuild = sub.add_parser("rebuild", help="export static sites for all cutoffs")
    p_rebuild.add_argument("--plan", required=True, help="path to plan.json")
    p_rebuild.add_argument("--warc", required=True, help="path to site.warc")
    p_rebuild.add_argument("--out", required=True, help="output directory")

    p_serve = sub.add_parser("serve", help="serve an exported site on 127.0.0.1")
    p_serve.add_argument("--site", required=True, help="exported site directory")
    p_serve.add_argument("--port", type=int, default=8000,
                         help="port to bind (0 = pick a free one)")

    p_verify = sub.add_parser("verify", help="verify exported sites without a browser")
    p_verify.add_argument("--plan", required=True)
    p_verify.add_argument("--warc", required=True)
    p_verify.add_argument("--out", required=True)
    p_verify.add_argument("--inventory", default=None,
                          help="optional capture-inventory.json to cross-check")
    p_verify.add_argument("--no-http", action="store_true",
                          help="skip the HTTP crawl checks")

    args = parser.parse_args(argv)

    if args.command == "rebuild":
        from .rebuild import rebuild

        results = rebuild(args.plan, args.warc, args.out)
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0
    if args.command == "serve":
        from .serve import serve

        serve(args.site, args.port)
        return 0
    if args.command == "verify":
        from .verify import main_verify

        return main_verify(args.plan, args.warc, args.out, args.inventory,
                           use_http=not args.no_http)
    return 2


if __name__ == "__main__":
    sys.exit(main())
