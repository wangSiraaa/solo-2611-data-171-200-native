"""端到端回归 + 对抗测试：python tests/test_pipeline.py

不依赖 pytest，纯标准库。覆盖：
  A. v1/v2 全流程产物结构与独立核验必须 0 error
  B. verify 必须能独立抓出坐标篡改、身份缺失、盒重叠、穿越、平行重合、自环退化
  C. 增量稳定性：未移动旧节点坐标逐点冻结、旧边折线逐点保留
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from depsvg.model import load_edges, load_nodes, load_plan          # noqa: E402
from depsvg.verify import verify                                     # noqa: E402

FX = os.path.join(ROOT, "fixtures")
OUT = os.path.join(ROOT, "outputs")


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "depsvg.cli", *args],
                          cwd=os.path.join(ROOT, "src"),
                          capture_output=True, text=True)


def codes(issues) -> set[str]:
    return {i.code for i in issues}


def main() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(("PASS" if cond else "FAIL"), name, detail)
        if not cond:
            failures.append(name)

    # ---- A. 全流程 -------------------------------------------------------
    r = run_cli("layout", "--nodes", f"{FX}/nodes-v1.json",
                "--edges", f"{FX}/edges-v1.csv", "--plan", f"{FX}/display-plan.json",
                "--svg", f"{OUT}/v1.svg", "--json", f"{OUT}/v1.json")
    check("v1 layout exit 0", r.returncode == 0, r.stderr[-300:])
    r = run_cli("layout", "--nodes", f"{FX}/nodes-v2.json",
                "--edges", f"{FX}/edges-v2.csv", "--plan", f"{FX}/display-plan.json",
                "--old-json", f"{OUT}/v1.json",
                "--svg", f"{OUT}/v2.svg", "--json", f"{OUT}/v2.json")
    check("v2 layout exit 0", r.returncode == 0, r.stderr[-300:])

    nodes1, edges1 = load_nodes(f"{FX}/nodes-v1.json"), load_edges(f"{FX}/edges-v1.csv")
    nodes2, edges2 = load_nodes(f"{FX}/nodes-v2.json"), load_edges(f"{FX}/edges-v2.csv")
    plan = load_plan(f"{FX}/display-plan.json")
    g1, s1 = json.load(open(f"{OUT}/v1.json")), open(f"{OUT}/v1.svg").read()
    g2, s2 = json.load(open(f"{OUT}/v2.json")), open(f"{OUT}/v2.svg").read()

    check("v1 verify clean", not verify(nodes1, edges1, g1, s1))
    check("v2 verify clean",
          not verify(nodes2, edges2, g2, s2, old_layout_json=g1))

    # 身份完整性
    check("v1 edge ids", {e["edge_id"] for e in g1["edges"]} ==
          {e.edge_id for e in edges1})
    check("v2 new nodes", {n["node_id"] for n in g2["nodes"] if n["is_new"]}
          == {"tax", "taxreport"})
    check("v2 new edges", {e["edge_id"] for e in g2["edges"] if e["is_new"]}
          == {"E15", "E16", "E17"})
    check("cycle kept", {e["edge_id"] for e in g1["edges"]
                         if e["kind"] == "backward"} == {"E05"})
    check("parallel marked",
          all(e["parallel_count"] == 2 for e in g1["edges"]
              if e["edge_id"] in ("E06", "E13")))

    # ---- B. 对抗：核验器必须抓错 -----------------------------------------
    bad_svg = s1.replace('<rect x="555.00"', '<rect x="560.00"', 1)
    check("catch svg/json mismatch",
          "svg-json-coordinate-mismatch" in codes(verify(nodes1, edges1, g1, bad_svg)))

    bad_json = copy.deepcopy(g1)
    bad_json["nodes"] = [n for n in bad_json["nodes"] if n["node_id"] != "raw"]
    check("catch missing node",
          "missing-node-json" in codes(verify(nodes1, edges1, bad_json, s1)))

    bad_json = copy.deepcopy(g1)
    for n in bad_json["nodes"]:
        if n["node_id"] == "alerts":
            n["box"]["y"] = next(x["box"]["y"] for x in g1["nodes"]
                                 if x["node_id"] == "agg")
    check("catch node overlap",
          "node-box-overlap" in codes(verify(nodes1, edges1, bad_json, s1)))

    bad_json = copy.deepcopy(g1)
    old_pts = next(e["points"] for e in g1["edges"] if e["edge_id"] == "E07")
    cross_pts = [[1000, 903], [650, 903], [650, 681], [650, 1184.5], [1120, 1184.5]]
    for e in bad_json["edges"]:
        if e["edge_id"] == "E07":
            e["points"] = cross_pts
    old_s = " ".join(f"{x:.2f},{y:.2f}" for x, y in old_pts)
    new_s = " ".join(f"{x:.2f},{y:.2f}" for x, y in cross_pts)
    check("catch edge crossing node",
          "edge-crosses-node" in codes(
              verify(nodes1, edges1, bad_json, s1.replace(f'points="{old_s}"',
                                                          f'points="{new_s}"'))))

    bad_json = copy.deepcopy(g1)
    e06 = next(e["points"] for e in g1["edges"] if e["edge_id"] == "E06")
    for e in bad_json["edges"]:
        if e["edge_id"] == "E13":
            e["points"] = copy.deepcopy(e06)
    check("catch parallel overlap",
          "parallel-edges-overlap" in codes(verify(nodes1, edges1, bad_json, s1)))

    bad_json = copy.deepcopy(g1)
    for e in bad_json["edges"]:
        if e["edge_id"] == "E14":
            e["points"] = e["points"][:2]
    check("catch degenerate self-loop",
          "self-loop-not-routed" in codes(verify(nodes1, edges1, bad_json, s1)))

    # ---- C. 增量稳定性 ----------------------------------------------------
    upd = g2["update"]
    moved = {d["node_id"] for d in upd["moved_nodes"]}
    old_box = {n["node_id"]: n["box"] for n in g1["nodes"]}
    frozen_ok = all(
        next(n["box"] for n in g2["nodes"] if n["node_id"] == nid) == old_box[nid]
        for nid in old_box if nid not in moved)
    check("unmoved old nodes frozen exactly", frozen_ok)
    check("moved nodes reported with cause",
          moved <= {"catalog", "audit"} and
          all(d["reason"] and d["dy"] != 0 for d in upd["moved_nodes"]))
    old_edges = {e["edge_id"]: e for e in g1["edges"]}
    new_edges = {e["edge_id"]: e for e in g2["edges"]}
    old_kept = all(old_edges[eid]["points"] == new_edges[eid]["points"]
                   for eid in old_edges if eid not in ("E12", "E14"))
    check("old edge polylines reused point-for-point", old_kept)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
