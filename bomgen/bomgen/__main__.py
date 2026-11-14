"""命令行: build(展开配置BOM并输出诊断) / verify(独立核对汇总)。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .build import run_build, verify_result
from .model import load_config, load_model
from .report import render_report


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bomgen", description="离线配置BOM生成器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="按销售配置展开BOM, 输出发生/零件/诊断/报告")
    b.add_argument("--definitions", required=True, help="装配定义 JSON")
    b.add_argument("--options", required=True, help="选项声明 JSON")
    b.add_argument("--constraints", required=True, help="跨装配约束 JSON")
    b.add_argument("--config", required=True, help="销售配置 JSON")
    b.add_argument("--out", required=True, help="输出目录")

    v = sub.add_parser("verify", help="从 occurrences 重新累加, 独立核对 parts 汇总")
    v.add_argument("--result", required=True, help="build 产出的 result.json")

    args = ap.parse_args(argv)
    if args.cmd == "build":
        return _build(args)
    return _verify(args)


def _build(args) -> int:
    try:
        model, mdiags = load_model(args.definitions, args.options, args.constraints)
        config, cdiags = load_config(model, args.config)
    except (OSError, ValueError) as e:
        print(f"输入错误: {e}", file=sys.stderr)
        return 2
    result = run_build(model, config, mdiags + cdiags)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(_dump(result), encoding="utf-8")
    (out / "occurrences.json").write_text(_dump(result["occurrences"]), encoding="utf-8")
    (out / "parts.json").write_text(_dump(result["parts"]), encoding="utf-8")
    (out / "diagnostics.json").write_text(_dump(result["diagnostics"]), encoding="utf-8")
    (out / "report.md").write_text(render_report(result), encoding="utf-8")

    print(render_report(result))
    s = result["summary"]
    print(f"---\n状态: {result['status']} | 发生 {s['occurrences']} | 零件 {s['parts']} | "
          f"错误 {s['errors']} | 警告 {s['warnings']} | 输出目录: {out}")
    return 0 if result["releasable"] else 3


def _verify(args) -> int:
    try:
        result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"输入错误: {e}", file=sys.stderr)
        return 2
    problems = verify_result(result)
    if problems:
        print("核对失败:")
        for p in problems:
            print(f"  - {p}")
        return 1
    n = len(result.get("parts", []))
    print(f"核对通过: {n} 个汇总零件均可从发生贡献重新累加, 且与 occurrences 一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
