"""命令行入口：python -m mvt_pipeline build|verify [参数...]。"""

from __future__ import annotations

import sys

from .build import main as build_main
from .verify import main as verify_main


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("build", "verify"):
        print("用法: python -m mvt_pipeline [build|verify] [参数...]", file=sys.stderr)
        return 2
    command = sys.argv[1]
    sys.argv = [f"mvt_pipeline {command}"] + sys.argv[2:]
    if command == "build":
        return build_main()
    return verify_main()


if __name__ == "__main__":
    raise SystemExit(main())
