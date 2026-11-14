"""命令行入口：python -m dimcheck [--json] FILE/DIR ..."""

from __future__ import annotations

import argparse
import json
import sys

from .report import check, explain, to_dict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dimcheck",
        description="受限 .dims/.formula 文本的量纲一致性检查（不做数值求解/单位换算）",
    )
    parser.add_argument("paths", nargs="+", help=".dims/.formula 文件或包含它们的目录")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON 而非说明文本")
    args = parser.parse_args(argv)

    result = check(args.paths)
    if args.json:
        json.dump(to_dict(result), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(explain(result))
        sys.stdout.write("\n")
    return 0 if result.status == "consistent" else 1


if __name__ == "__main__":
    raise SystemExit(main())
