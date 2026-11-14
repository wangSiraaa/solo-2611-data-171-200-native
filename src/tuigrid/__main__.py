"""命令行入口：

    python3 -m tuigrid --grid fixtures/grid.json --schema fixtures/schema.json \
        [--export artifacts/export.json]

--export 给出 F2 导出目标路径；不给定时程序仍可运行，按 F2 会提示缺少路径。
"""

from __future__ import annotations

import argparse
import sys

from .app import TuiApp
from .model import GridModel


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tuigrid",
        description="curses 20×8 小表 bracketed-paste 粘贴编辑器（Python 3.11 标准库）",
    )
    parser.add_argument("--grid", required=True, help="grid.json 路径（20 行 8 列）")
    parser.add_argument("--schema", required=True, help="schema.json 路径")
    parser.add_argument(
        "--export",
        dest="export_path",
        default=None,
        help="F2 导出当前表的目标 JSON 路径",
    )
    args = parser.parse_args(argv)

    try:
        model = GridModel.load(args.grid, args.schema)
    except (OSError, ValueError, KeyError) as exc:
        print(f"启动失败：无法加载网格/schema：{exc}", file=sys.stderr)
        return 2

    TuiApp(model, export_path=args.export_path).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
