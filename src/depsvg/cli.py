"""命令行：layout / verify / report 三个子命令。

用法：
  python -m depsvg.cli layout   --nodes ... --edges ... --plan ... --svg out.svg --json out.json
  python -m depsvg.cli layout   ... --old-json v1.json   （增量模式）
  python -m depsvg.cli verify   --nodes ... --edges ... --plan ... --json out.json --svg out.svg [--old-json v1.json]
  python -m depsvg.cli report   --v1-json ... --v1-svg ... --v2-json ... --v2-svg ... --out compare.html
"""
from __future__ import annotations

import argparse
import json
import sys

from .incremental import IncrementalEngine
from .layout import Engine
from .model import Graph, load_edges, load_nodes, load_plan
from .svg import render_svg
from .verify import verify


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def cmd_layout(args) -> int:
    nodes = load_nodes(args.nodes)
    edges = load_edges(args.edges)
    plan = load_plan(args.plan)
    graph = Graph(nodes, edges)

    if args.old_json:
        old = json.load(open(args.old_json, encoding="utf-8"))
        old_nodes = {n["node_id"] for n in old["nodes"]}
        old_edges = {e["edge_id"] for e in old["edges"]}
        new_nodes = {n.node_id for n in nodes if n.node_id not in old_nodes}
        new_edges = {e.edge_id for e in edges if e.edge_id not in old_edges}
        engine = IncrementalEngine(graph, plan, old, new_nodes, new_edges)
        mode = "incremental"
    else:
        engine = Engine(graph, plan)
        mode = "full"

    layout = engine.run()
    layout.source_files = {"nodes": args.nodes, "edges": args.edges,
                           "plan": args.plan, "mode": mode,
                           **({"old_json": args.old_json} if args.old_json else {})}
    svg = render_svg(layout)
    _write(args.svg, svg)
    _write(args.json_out, json.dumps(layout.to_dict(), ensure_ascii=False, indent=2))

    errors = [i for i in layout.issues if i.severity == "error"]
    warnings = [i for i in layout.issues if i.severity == "warning"]
    infos = [i for i in layout.issues if i.severity == "info"]
    print(f"[{mode}] 节点 {len(layout.nodes)} 个，边 {len(layout.edges)} 条，"
          f"SCC 簇 {len(layout.sccs)} 个，组件 {len(layout.components)} 个")
    print(f"画布 {layout.canvas.width:.0f} x {layout.canvas.height:.0f}，"
          f"输出 {args.svg} / {args.json_out}")
    for iss in layout.issues:
        print(f"  {iss.severity.upper():7} {iss.code}: {iss.message}")
    if errors:
        print(f"布局存在 {len(errors)} 个错误、{len(warnings)} 个警告、"
              f"{len(infos)} 条信息。")
        return 2
    print(f"布局完成：0 错误，{len(warnings)} 警告，{len(infos)} 条信息"
          "（警告/信息均带 ID 可定位）。")
    return 0


def cmd_verify(args) -> int:
    nodes = load_nodes(args.nodes)
    edges = load_edges(args.edges)
    layout_json = json.load(open(args.json_out, encoding="utf-8"))
    svg_text = open(args.svg, encoding="utf-8").read()
    old = json.load(open(args.old_json, encoding="utf-8")) if args.old_json else None
    issues = verify(nodes, edges, layout_json, svg_text, old)
    counts = {"error": 0, "warning": 0, "info": 0}
    for iss in issues:
        counts[iss.severity] += 1
        print(f"{iss.severity.upper():7} {iss.code}: {iss.message}")
    print(f"\n独立核验结论：{counts['error']} 错误，{counts['warning']} 警告，"
          f"{counts['info']} 信息（坐标取自实际 SVG 文本并与 JSON 逐项比对）")
    if args.report:
        _write(args.report, json.dumps(
            {"counts": counts,
             "issues": [i.to_dict() for i in issues]},
            ensure_ascii=False, indent=2))
    return 1 if counts["error"] else 0


def cmd_report(args) -> int:
    from .report import build_html
    html = build_html(args.v1_json, args.v1_svg, args.v2_json, args.v2_svg)
    _write(args.out, html)
    print(f"对照 HTML 已写入 {args.out}（内联 SVG，无外部依赖，可直接 file:// 打开）")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="depsvg",
                                description="依赖关系图静态 SVG 层次布局生成器")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("layout", help="生成布局 SVG 与坐标 JSON")
    pl.add_argument("--nodes", required=True)
    pl.add_argument("--edges", required=True)
    pl.add_argument("--plan", required=True)
    pl.add_argument("--svg", required=True)
    pl.add_argument("--json", dest="json_out", required=True)
    pl.add_argument("--old-json", help="传入旧布局 JSON 即进入增量模式")
    pl.set_defaults(func=cmd_layout)

    pv = sub.add_parser("verify", help="独立核验布局产物")
    pv.add_argument("--nodes", required=True)
    pv.add_argument("--edges", required=True)
    pv.add_argument("--plan", required=True)
    pv.add_argument("--json", dest="json_out", required=True)
    pv.add_argument("--svg", required=True)
    pv.add_argument("--old-json")
    pv.add_argument("--report", help="核验结果 JSON 输出路径")
    pv.set_defaults(func=cmd_verify)

    pr = sub.add_parser("report", help="生成 v1/v2 对照 HTML")
    pr.add_argument("--v1-json", required=True)
    pr.add_argument("--v1-svg", required=True)
    pr.add_argument("--v2-json", required=True)
    pr.add_argument("--v2-svg", required=True)
    pr.add_argument("--out", required=True)
    pr.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
