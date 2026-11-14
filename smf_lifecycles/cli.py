"""命令行入口：

    python -m smf_lifecycles convert <file.mid> [-o OUT_DIR] [--prefix NAME]
    python -m smf_lifecycles verify  <file.mid> [--expected expected.json]

convert 产出：
  <prefix>_analysis.json   完整分析（音符生命周期、踏板证据、诊断、tempo 映射、事件流）
  <prefix>_notes.csv       音符生命周期表
  <prefix>_diagnostics.csv 诊断表
  <prefix>_timeline.html   本地可打开的简洁时间线（无外部依赖）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analyze import analyze, write_csv, write_html, write_json
from .verify import verify


def _cmd_convert(args: argparse.Namespace) -> int:
    result = analyze(args.midi_file)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = args.prefix or Path(args.midi_file).stem

    json_path = out_dir / f"{prefix}_analysis.json"
    notes_csv = out_dir / f"{prefix}_notes.csv"
    diag_csv = out_dir / f"{prefix}_diagnostics.csv"
    html_path = out_dir / f"{prefix}_timeline.html"

    write_json(result, json_path)
    write_csv(result, notes_csv, diag_csv)
    write_html(result, html_path)

    s = result["summary"]
    print(f"[convert] {args.midi_file}")
    print(f"  JSON       -> {json_path}")
    print(f"  音符 CSV   -> {notes_csv}  ({s['note_count']} 个音符, "
          f"{s['truncated_notes']} 个 EOF 截断)")
    print(f"  诊断 CSV   -> {diag_csv}  ({len(result['diagnostics'])} 条)")
    print(f"  时间线 HTML-> {html_path}")
    for d in result["diagnostics"]:
        print(f"    - [{d['code']}] tick {d['tick']}: {d['message']}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    report = verify(args.midi_file, args.expected)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["all_passed"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smf_lifecycles",
        description="SMF 格式 1 音符生命周期离线解析（不合成音频）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_conv = sub.add_parser("convert", help="解析 MIDI 并输出 JSON/CSV/HTML")
    p_conv.add_argument("midi_file")
    p_conv.add_argument("-o", "--out-dir", default="outputs")
    p_conv.add_argument("--prefix", default=None)
    p_conv.set_defaults(func=_cmd_convert)

    p_ver = sub.add_parser("verify", help="重新解析并核验不变量/期望值")
    p_ver.add_argument("midi_file")
    p_ver.add_argument("--expected", default=None,
                       help="convert 产出的期望 analysis JSON")
    p_ver.set_defaults(func=_cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
