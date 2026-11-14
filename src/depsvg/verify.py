"""独立核验：不依赖布局器内部状态，只读取输入图、坐标 JSON 与实际 SVG。

检查项：
  1. 身份完整性：每个 node_id / edge_id 都在 JSON 与 SVG 中且一一对应
  2. SVG/JSON 坐标一致：节点盒、折线点、边标签盒逐项核对（同一套坐标）
  3. 节点盒重叠（共边/共点单独警告）
  4. 折线与无关节点盒相交；箭头端点落在源/目标盒边界
  5. 标签遮挡：节点标签在盒内、边标签不压任何节点盒、边标签互不重叠
  6. 平行边不重合、自环为显式折线
  7. 增量模式：位移报告与真实位移一致，未报告的旧节点必须坐标冻结
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from .model import Box, Issue, r
from .text import (boxes_overlap, boxes_touch, edge_label_width,
                   segment_intersects_box, text_width)

NS = "{http://www.w3.org/2000/svg}"


def _parse_points(s: str) -> list[tuple[float, float]]:
    out = []
    for tok in s.strip().split():
        x, y = tok.split(",")
        out.append((float(x), float(y)))
    return out


def parse_svg(svg_text: str) -> dict:
    """从实际 SVG 文本解析出渲染元素，坐标直接取画面中的值。"""
    root = ET.fromstring(svg_text)
    nodes, edges, labels = {}, {}, {}
    for g in root.iter(NS + "g"):
        cls = g.get("class", "")
        if "node" in cls.split():
            nid = g.get("data-node-id")
            rect = g.find(NS + "rect")
            nodes[nid] = Box(float(rect.get("x")), float(rect.get("y")),
                             float(rect.get("width")), float(rect.get("height")))
        elif "edge" in cls.split() and "edge-label" not in cls:
            eid = g.get("data-edge-id")
            pl = g.find(NS + "polyline")
            edges[eid] = {
                "points": _parse_points(pl.get("points")),
                "marker": pl.get("marker-end", ""),
            }
        elif "edge-label" in cls.split():
            eid = g.get("data-edge-id")
            rect = g.find(NS + "rect")
            text = g.find(NS + "text")
            labels[eid] = {
                "box": Box(float(rect.get("x")), float(rect.get("y")),
                           float(rect.get("width")), float(rect.get("height"))),
                "text": text.text or "",
            }
    return {"nodes": nodes, "edges": edges, "labels": labels,
            "width": float(root.get("width")),
            "height": float(root.get("height"))}


def verify(input_nodes, input_edges, layout_json: dict, svg_text: str,
           old_layout_json: dict | None = None) -> list[Issue]:
    issues: list[Issue] = []
    svg = parse_svg(svg_text)
    in_nodes = {n.node_id for n in input_nodes}
    in_edges = {e.edge_id: e for e in input_edges}
    j_nodes = {n["node_id"]: n for n in layout_json["nodes"]}
    j_edges = {e["edge_id"]: e for e in layout_json["edges"]}

    # 1. 身份完整性 ---------------------------------------------------------
    for nid in in_nodes:
        if nid not in j_nodes:
            issues.append(Issue("error", "missing-node-json",
                                f"输入节点 {nid} 不在坐标 JSON 中", [nid]))
        if nid not in svg["nodes"]:
            issues.append(Issue("error", "missing-node-svg",
                                f"输入节点 {nid} 不在 SVG 中", [nid]))
    extra_j = set(j_nodes) - in_nodes
    extra_s = set(svg["nodes"]) - in_nodes
    for nid in sorted(extra_j):
        issues.append(Issue("error", "extra-node-json",
                            f"JSON 出现输入中不存在的节点 {nid}", [nid]))
    for nid in sorted(extra_s):
        issues.append(Issue("error", "extra-node-svg",
                            f"SVG 出现输入中不存在的节点 {nid}", [nid]))
    for eid, e in in_edges.items():
        if eid not in j_edges:
            issues.append(Issue("error", "missing-edge-json",
                                f"输入边 {eid} 不在坐标 JSON 中", [eid]))
        if eid not in svg["edges"]:
            issues.append(Issue("error", "missing-edge-svg",
                                f"输入边 {eid} 不在 SVG 中", [eid]))
        if eid in j_edges and (j_edges[eid]["source"] != e.source or
                               j_edges[eid]["target"] != e.target):
            issues.append(Issue("error", "edge-identity-mismatch",
                                f"边 {eid} 的端点身份与输入不一致", [eid]))
    for eid in sorted(set(j_edges) - set(in_edges)):
        issues.append(Issue("error", "extra-edge-json",
                            f"JSON 出现输入中不存在的边 {eid}", [eid]))
    for eid in sorted(set(svg["edges"]) - set(in_edges)):
        issues.append(Issue("error", "extra-edge-svg",
                            f"SVG 出现输入中不存在的边 {eid}", [eid]))

    # 2. SVG 与 JSON 使用同一套坐标 ----------------------------------------
    for nid, jn in j_nodes.items():
        if nid not in svg["nodes"]:
            continue
        jb, sb = Box(**jn["box"]), svg["nodes"][nid]
        for name, a, b in (("x", jb.x, sb.x), ("y", jb.y, sb.y),
                           ("width", jb.width, sb.width),
                           ("height", jb.height, sb.height)):
            if abs(a - b) > 0.01:
                issues.append(Issue(
                    "error", "svg-json-coordinate-mismatch",
                    f"节点 {nid} 的 {name}：JSON={r(a)} 但 SVG={r(b)}，"
                    "画的与报的不是同一套坐标", [nid]))
    for eid, je in j_edges.items():
        if eid not in svg["edges"]:
            continue
        sp = svg["edges"][eid]["points"]
        jp = [tuple(p) for p in je["points"]]
        if len(sp) != len(jp):
            issues.append(Issue("error", "svg-json-polyline-mismatch",
                                f"边 {eid} 折线段数 JSON={len(jp)} SVG={len(sp)}",
                                [eid]))
        else:
            for k, (a, b) in enumerate(zip(jp, sp)):
                if abs(a[0] - b[0]) > 0.01 or abs(a[1] - b[1]) > 0.01:
                    issues.append(Issue(
                        "error", "svg-json-polyline-mismatch",
                        f"边 {eid} 第 {k} 点 JSON={a} 但 SVG={b}", [eid]))
        if "url(#arrow)" not in svg["edges"][eid]["marker"]:
            issues.append(Issue("error", "missing-arrow-marker",
                                f"边 {eid} 缺少箭头标记", [eid]))
        jlb = je.get("label_box")
        slb = svg["labels"].get(eid)
        if jlb is None and slb is not None:
            issues.append(Issue("error", "svg-json-label-mismatch",
                                f"边 {eid} SVG 有标签但 JSON 无标签盒", [eid]))
        elif jlb is not None:
            if slb is None:
                issues.append(Issue("error", "svg-json-label-mismatch",
                                    f"边 {eid} JSON 有标签盒但 SVG 未渲染", [eid]))
            else:
                jb = Box(**jlb["box"])
                for name, a, b in (("x", jb.x, slb["box"].x),
                                   ("y", jb.y, slb["box"].y),
                                   ("width", jb.width, slb["box"].width),
                                   ("height", jb.height, slb["box"].height)):
                    if abs(a - b) > 0.01:
                        issues.append(Issue(
                            "error", "svg-json-label-mismatch",
                            f"边 {eid} 标签盒 {name} JSON={r(a)} SVG={r(b)}",
                            [eid]))
                if slb["text"] != jlb["text"]:
                    issues.append(Issue("error", "svg-json-label-mismatch",
                                        f"边 {eid} 标签文本不一致", [eid]))
                # 宽度规则复核
                expect_w = edge_label_width(jlb["text"])
                if abs(expect_w - jb.width) > 0.01:
                    issues.append(Issue(
                        "warning", "label-width-rule",
                        f"边 {eid} 标签宽 {r(jb.width)} 与 display-plan 规则值 "
                        f"{r(expect_w)} 不符", [eid]))

    node_boxes = {nid: Box(**jn["box"]) for nid, jn in j_nodes.items()}

    # 3. 节点盒重叠 ---------------------------------------------------------
    ids = sorted(node_boxes)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = node_boxes[ids[i]], node_boxes[ids[j]]
            if boxes_overlap(a, b):
                issues.append(Issue(
                    "error", "node-box-overlap",
                    f"节点 {ids[i]} 与 {ids[j]} 盒重叠", [ids[i], ids[j]]))
            elif boxes_touch(a, b):
                issues.append(Issue(
                    "warning", "node-box-touch",
                    f"节点 {ids[i]} 与 {ids[j]} 盒共边/共点（无间隙）",
                    [ids[i], ids[j]]))

    # 4. 折线-节点相交 & 箭头端点 -------------------------------------------
    for eid, je in j_edges.items():
        e = in_edges[eid]
        pts = [tuple(p) for p in je["points"]]
        allowed = {e.source} if je["is_self_loop"] else {e.source, e.target}
        for nid, nb in node_boxes.items():
            for k in range(len(pts) - 1):
                hits = segment_intersects_box(pts[k], pts[k + 1], nb)
                for pt in hits:
                    endpoint = any(abs(pt[0] - q[0]) < 0.6 and
                                   abs(pt[1] - q[1]) < 0.6
                                   for q in (pts[0], pts[-1]))
                    if nid not in allowed and not endpoint:
                        issues.append(Issue(
                            "error", "edge-crosses-node",
                            f"边 {eid} 折线与无关节点 {nid} 相交于 {pt}",
                            [eid, nid], {"at": {"x": pt[0], "y": pt[1]}}))
        if e.source not in node_boxes or e.target not in node_boxes:
            continue            # 端点节点缺失已在身份完整性中报错
        # 端点必须在对应节点盒边界上
        sb, tb = node_boxes[e.source], node_boxes[e.target]
        p_start, p_end = pts[0], pts[-1]
        for name, pt, box, nid in (("起点", p_start, sb, e.source),
                                   ("终点", p_end, tb, e.target)):
            on_boundary = (
                (abs(pt[0] - box.x) < 0.6 or abs(pt[0] - box.x2) < 0.6)
                and box.y - 0.6 <= pt[1] <= box.y2 + 0.6) or (
                (abs(pt[1] - box.y) < 0.6 or abs(pt[1] - box.y2) < 0.6)
                and box.x - 0.6 <= pt[0] <= box.x2 + 0.6)
            if not on_boundary:
                issues.append(Issue(
                    "error", "arrow-endpoint-off-box",
                    f"边 {eid} {name} {pt} 未落在节点 {nid} 盒边界上",
                    [eid, nid]))
        if je["is_self_loop"] and len(pts) < 4:
            issues.append(Issue("error", "self-loop-not-routed",
                                f"自环 {eid} 不是显式多段折线", [eid]))

    # 5. 平行边不得完全重合 -------------------------------------------------
    groups: dict[tuple[str, str], list[str]] = {}
    for eid, e in in_edges.items():
        groups.setdefault((e.source, e.target), []).append(eid)
    for key, eids in groups.items():
        if len(eids) < 2:
            continue
        paths = {eid: [tuple(p) for p in j_edges[eid]["points"]] for eid in eids}
        for i in range(len(eids)):
            for j in range(i + 1, len(eids)):
                if paths[eids[i]] == paths[eids[j]]:
                    issues.append(Issue(
                        "error", "parallel-edges-overlap",
                        f"平行边 {eids[i]} 与 {eids[j]} 折线完全重合，"
                        "无法看出边数量", [eids[i], eids[j]]))

    # 6. 标签遮挡 -----------------------------------------------------------
    # 6a. 节点换行文字必须落在自身盒内
    for nid, jn in j_nodes.items():
        nb = Box(**jn["box"])
        widest = max((text_width(ln) for ln in jn["lines"]), default=0.0)
        if widest > nb.width - 2:       # 留 1px 容差
            issues.append(Issue(
                "warning", "node-label-overflow",
                f"节点 {nid} 标签行宽 {r(widest)} 超出盒宽 {r(nb.width)}", [nid]))
    # 6b. 边标签不压节点盒、互不重叠
    label_boxes: dict[str, Box] = {}
    for eid, je in j_edges.items():
        if je.get("label_box"):
            label_boxes[eid] = Box(**je["label_box"]["box"])
    for eid, lb in label_boxes.items():
        for nid, nb in node_boxes.items():
            if boxes_overlap(lb, nb):
                issues.append(Issue(
                    "error", "edge-label-covers-node",
                    f"边 {eid} 标签盒压在节点 {nid} 上", [eid, nid]))
    eids = sorted(label_boxes)
    for i in range(len(eids)):
        for j in range(i + 1, len(eids)):
            if boxes_overlap(label_boxes[eids[i]], label_boxes[eids[j]]):
                issues.append(Issue(
                    "warning", "edge-label-overlap",
                    f"边标签 {eids[i]} 与 {eids[j]} 互相遮挡",
                    [eids[i], eids[j]]))

    # 7. 增量更新位移复核 ---------------------------------------------------
    if old_layout_json is not None:
        upd = layout_json.get("update")
        if upd is None:
            issues.append(Issue("error", "missing-update-report",
                                "增量布局缺少 update 报告"))
        else:
            old_boxes = {n["node_id"]: Box(**n["box"])
                         for n in old_layout_json["nodes"]}
            reported = {d["node_id"]: d for d in upd["moved_nodes"]}
            for nid, ob in old_boxes.items():
                if nid not in node_boxes:
                    continue
                nb = node_boxes[nid]
                dx, dy = r(nb.x - ob.x), r(nb.y - ob.y)
                moved = abs(dx) > 0.01 or abs(dy) > 0.01
                if moved and nid not in reported:
                    issues.append(Issue(
                        "error", "unreported-displacement",
                        f"旧节点 {nid} 实际位移 ({dx},{dy}) 但未报告", [nid]))
                if not moved and nid in reported:
                    issues.append(Issue(
                        "error", "false-displacement-report",
                        f"节点 {nid} 报告了位移但坐标未变", [nid]))
            # 未移动旧边应逐点保留
            old_edges = {e["edge_id"]: e for e in old_layout_json["edges"]}
            for eid, oe in old_edges.items():
                if eid not in j_edges or eid in reported:
                    continue
                moved_related = any(n in reported for n in (oe["source"], oe["target"]))
                if moved_related:
                    continue
                old_pts = [tuple(p) for p in oe["points"]]
                new_pts = [tuple(p) for p in j_edges[eid]["points"]]
                if old_pts != new_pts:
                    issues.append(Issue(
                        "warning", "old-edge-rerouted",
                        f"旧边 {eid} 端点未移动但折线被重新路由", [eid]))

    # 8. 画布必须容纳所有元素 ----------------------------------------------
    for nid, nb in node_boxes.items():
        if nb.x2 > layout_json["canvas"]["width"] + 0.01 or \
           nb.y2 > layout_json["canvas"]["height"] + 0.01 or \
           nb.x < -0.01 or nb.y < -0.01:
            issues.append(Issue("error", "element-outside-canvas",
                                f"节点 {nid} 超出画布范围", [nid]))
    if abs(svg["width"] - layout_json["canvas"]["width"]) > 0.01 or \
       abs(svg["height"] - layout_json["canvas"]["height"]) > 0.01:
        issues.append(Issue("error", "svg-json-canvas-mismatch",
                            "SVG 画布尺寸与 JSON canvas 不一致"))
    return issues
