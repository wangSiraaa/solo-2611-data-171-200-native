"""``python -m depresolver`` -> subcommands ``resolve`` and ``verify``."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print(
            "usage: python -m depresolver {resolve,verify,audit} [options]\n"
            "  resolve  wheelhouse + requests -> lock files and reports\n"
            "  verify   install lock files with --no-index --no-deps and inspect\n"
            "  audit    independently re-derive the closure and check constraints"
        )
        return 0
    command, rest = argv[0], argv[1:]
    if command == "resolve":
        from .resolve import main as resolve_main

        return resolve_main(rest)
    if command == "verify":
        from .verify import main as verify_main

        return verify_main(rest)
    if command == "audit":
        from .audit import main as audit_main

        return audit_main(rest)
    print(f"unknown command {command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
