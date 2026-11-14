"""structedit 命令行：edit / verify。

用法：
  python -m structedit edit   --source fixtures/model.xlsx \
      --operations fixtures/operations.json --output out/edited.xlsx \
      --report out/report.json
  python -m structedit verify --source fixtures/model.xlsx \
      --operations fixtures/operations.json --result out/edited.xlsx \
      --report out/report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import run_edit
from .formula import FormulaError, UnsupportedConstruct
from .verify import verify


def _load_operations(path: str) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cmd_edit(args: argparse.Namespace) -> int:
    operations = _load_operations(args.operations)
    try:
        report = run_edit(args.source, operations, args.output, args.report)
    except UnsupportedConstruct as exc:
        print(f"拒绝本次编辑：{exc}", file=sys.stderr)
        return 2
    except (FormulaError, ValueError) as exc:
        print(f"编辑失败：{exc}", file=sys.stderr)
        return 2
    changed = [f for f in report["formulas"] if f["status"] == "changed"]
    deleted = [f for f in report["formulas"] if f["status"] == "deleted"]
    print(f"已生成结果副本: {report['output']['path']}")
    print(f"  源 sha256   : {report['source']['sha256']}")
    print(f"  输出 sha256 : {report['output']['sha256']}")
    print(f"  公式 {len(report['formulas'])} 个：变更 {len(changed)}，"
          f"被删 {len(deleted)}，不变 {len(report['formulas']) - len(changed) - len(deleted)}")
    for entry in report["formulas"]:
        if entry["status"] != "unchanged":
            print(f"    {entry['sheet']}!{entry['cell_before']}"
                  f"{'→' + entry['cell_after'] if entry['cell_after'] else ''}: "
                  f"{entry['before']} → {entry['after']}")
    for entry in report["defined_names"]:
        mark = "" if entry["status"] == "changed" else "（拼写不变）"
        print(f"  名称 {entry['name']}（{entry['scope']}）: "
              f"{entry['before']} → {entry['after']} {mark}")
    print(f"  重算状态: fullCalcOnLoad=1 —— {report['recalculation']['note']}")
    if args.report:
        print(f"  详细报告: {args.report}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    operations = _load_operations(args.operations)
    try:
        rep = verify(args.source, operations, args.result, args.report)
    except (FormulaError, UnsupportedConstruct, ValueError) as exc:
        print(f"验证中断：{exc}", file=sys.stderr)
        return 2
    for chk in rep.checks:
        mark = "PASS" if chk["ok"] else "FAIL"
        line = f"[{mark}] {chk['check']}"
        if chk["detail"]:
            line += f" —— {chk['detail']}"
        print(line)
    print(f"\n验证{'通过' if rep.ok else '失败'}："
          f"{sum(1 for c in rep.checks if c['ok'])}/{len(rep.checks)} 项通过")
    return 0 if rep.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="structedit", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_edit = sub.add_parser("edit", help="执行结构编辑并产出结果副本与报告")
    p_edit.add_argument("--source", required=True)
    p_edit.add_argument("--operations", required=True)
    p_edit.add_argument("--output", required=True)
    p_edit.add_argument("--report")
    p_edit.set_defaults(func=cmd_edit)

    p_verify = sub.add_parser("verify", help="重新读取结果 XLSX 独立核对")
    p_verify.add_argument("--source", required=True)
    p_verify.add_argument("--operations", required=True)
    p_verify.add_argument("--result", required=True)
    p_verify.add_argument("--report")
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
