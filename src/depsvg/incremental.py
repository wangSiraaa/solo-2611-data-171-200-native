"""增量布局：读取 v1 坐标 JSON，固定未受影响节点，只安排新增节点/边。

策略与取舍：
  - 旧节点坐标默认原样保留；仅当新增节点在旧组件下方没有不重叠位置、
    必须推开后续独立组件时，才对被推开的整个组件做刚体平移并逐节点报告位移。
  - 旧边折线原样保留，不重新路由，保证前后图形可逐点对照。
  - 新节点按图上最长路径锚定到旧层号；新边复用同一套通道/走廊/自环路由。
"""
from __future__ import annotations

from collections import defaultdict

from .layout import (CLUSTER_PAD_X, CLUSTER_PAD_Y, COMP_GAP, Engine,
                     NODE_GAP, RANK_GAP)
from .model import (Box, ComponentView, EdgeView, Graph, Issue, LabelBox,
                    Layout, NodeView, SCCView, DisplayPlan, r)
from .text import boxes_overlap, edge_label_width, needed_box_size


class IncrementalEngine(Engine):
    def __init__(self, graph: Graph, plan: DisplayPlan,
                 old_layout: dict,
                 new_node_ids: set[str], new_edge_ids: set[str]):
        super().__init__(graph, plan, new_node_ids, new_edge_ids)
        self.old_layout = old_layout
        self.old_node_boxes = {n["node_id"]: Box(**n["box"])
                               for n in old_layout["nodes"]}
        self.old_edges = {e["edge_id"]: e for e in old_layout["edges"]}
        self.displacements: list[dict] = []
        self.relocation_causes: list[str] = []
        # 组件 -> 累计刚体平移量；_place 记录，_route 建完旧边后再施加
        self.pending_shifts: dict[int, float] = defaultdict(float)

    # -- 覆盖尺寸/定位流程：旧节点冻结，新节点插入 --------------------------
    def _prepare_sizes(self) -> None:
        for n in self.g.nodes:
            if n.node_id in self.new_nodes:
                w, h, lines, enlarged = needed_box_size(
                    n.label, n.width, n.height, self.plan.node_padding)
                box = Box(0, 0, w, h)
                if enlarged:
                    self.issues.append(Issue(
                        "info", "node-box-enlarged",
                        f"新节点 {n.node_id} 给定盒放不下标签，已放大至 "
                        f"{r(w)}x{r(h)}", [n.node_id]))
            else:
                old = self.old_node_boxes[n.node_id]
                # 尺寸以旧坐标 JSON 为准（含 v1 的放大结果），换行重算仅用于渲染
                _w, _h, lines, _en = needed_box_size(
                    n.label, old.width, old.height, self.plan.node_padding)
                w, h, box = old.width, old.height, Box(old.x, old.y,
                                                       old.width, old.height)
                enlarged = False
            nv = NodeView(
                node_id=n.node_id, label=n.label, box=box, lines=lines,
                box_enlarged=enlarged,
                component=self.a.node_component[n.node_id],
                rank=self.a.node_rank[n.node_id],
                scc_id=self.a.node_scc[n.node_id],
                in_cycle=self.a.cyclic[self.a.node_scc[n.node_id]],
                is_new=n.node_id in self.new_nodes)
            self.nodes[n.node_id] = nv

    def _place(self) -> None:
        # 1) 旧节点冻结，重建旧组件盒
        comp_boxes: dict[int, Box] = {}
        comp_nodes: dict[int, list[str]] = defaultdict(list)
        for nid, nv in self.nodes.items():
            if not nv.is_new:
                comp_nodes[nv.component].append(nid)
        for cid, ids in comp_nodes.items():
            boxes = [self.nodes[i].box for i in ids]
            comp_boxes[cid] = Box(
                min(b.x for b in boxes), min(b.y for b in boxes),
                max(b.x2 for b in boxes) - min(b.x for b in boxes),
                max(b.y2 for b in boxes) - min(b.y for b in boxes))

        # 2) 复原旧层中心 x；新层沿层方向按 RANK_GAP 外延
        old_rank_x = {int(k): v for k, v in
                      self.old_layout.get("rank_center_x", {}).items()}
        for key, val in old_rank_x.items():
            self.rank_x[(key // 1000, key % 1000)] = val
        for nid in sorted(self.new_nodes):
            nv = self.nodes[nid]
            key = (nv.component, nv.rank)
            if key in self.rank_x:
                continue
            prev = max(rk for (c, rk) in self.rank_x if c == nv.component)
            prev_x = self.rank_x[(nv.component, prev)]
            self.rank_x[key] = prev_x + RANK_GAP + nv.box.width / 2

        # 3) 新节点：按 (组件,层) 分组，放在旧组件块下方，x 与所属层列对齐
        new_by_comp_rank: dict[tuple[int, int], list[NodeView]] = defaultdict(list)
        for nid in sorted(self.new_nodes):
            new_by_comp_rank[(self.nodes[nid].component,
                              self.nodes[nid].rank)].append(self.nodes[nid])

        frozen_boxes = [nv.box for nid, nv in self.nodes.items() if not nv.is_new]
        max_new_bottom: dict[int, float] = {}
        for (cid, rk), new_views in sorted(new_by_comp_rank.items()):
            anchor = comp_boxes[cid]
            block_h = sum(nv.box.height for nv in new_views) + \
                NODE_GAP * (len(new_views) - 1)
            cx = self.rank_x[(cid, rk)]
            y = anchor.y2 + COMP_GAP
            placed = False
            for _try in range(20):                       # 有界重试避开冻结盒
                trial_boxes, ty = [], y
                for nv in new_views:
                    trial_boxes.append(Box(cx - nv.box.width / 2, ty,
                                           nv.box.width, nv.box.height))
                    ty += nv.box.height + NODE_GAP
                if not any(boxes_overlap(tb, fb)
                           for tb in trial_boxes for fb in frozen_boxes):
                    for nv, tb in zip(new_views, trial_boxes):
                        nv.box.x, nv.box.y = tb.x, tb.y
                        frozen_boxes.append(tb)
                    placed = True
                    break
                y += NODE_GAP + 10
            if not placed:
                raise RuntimeError(f"新节点层 ({cid},{rk}) 找不到不重叠位置")
            max_new_bottom[cid] = max(max_new_bottom.get(cid, 0.0),
                                      y + block_h)
            comp_boxes[cid] = Box(
                anchor.x, anchor.y, anchor.width,
                max(anchor.y2, y + block_h) - anchor.y)

        # 4) 一次性计算对后续独立组件的总下推量，整体刚体平移（只移一次）
        for cid, new_bottom in max_new_bottom.items():
            later = sorted(c for c in comp_boxes if c > cid)
            if not later:
                continue
            next_top = min(comp_boxes[c].y for c in later)
            overflow = new_bottom + COMP_GAP - next_top
            if overflow > 0:
                self._shift_components(later, overflow,
                                       reason=(f"新增节点底部 {r(new_bottom)} 与下游"
                                               f"独立组件（顶部 {r(next_top)}）间距"
                                               f"不足 {r(COMP_GAP)}px"))

        # 5) 重建组件视图 / SCC 簇框
        all_comp_nodes: dict[int, list[str]] = defaultdict(list)
        for nid, nv in self.nodes.items():
            all_comp_nodes[nv.component].append(nid)
        self.comp_views = []
        for cid in sorted(all_comp_nodes):
            ids = all_comp_nodes[cid]
            boxes = [self.nodes[i].box for i in ids]
            self.comp_views.append(ComponentView(
                cid, sorted(ids),
                Box(min(b.x for b in boxes), min(b.y for b in boxes),
                    max(b.x2 for b in boxes) - min(b.x for b in boxes),
                    max(b.y2 for b in boxes) - min(b.y for b in boxes))))
        self._rebuild_scc_views()

        self.canvas_right = max(c.box.x2 for c in self.comp_views) + 30
        self.canvas_bottom = max(c.box.y2 for c in self.comp_views) + 30

    def _rebuild_scc_views(self) -> None:
        self.scc_views = []
        for sid, ms in enumerate(self.a.scc_nodes):
            if not self.a.cyclic[sid]:
                continue
            boxes = [self.nodes[m].box for m in ms]
            x0 = min(b.x for b in boxes) - CLUSTER_PAD_X
            y0 = min(b.y for b in boxes) - CLUSTER_PAD_Y
            x1 = max(b.x2 for b in boxes) + CLUSTER_PAD_X
            y1 = max(b.y2 for b in boxes) + CLUSTER_PAD_Y
            self.scc_views.append(SCCView(
                sid, list(ms), Box(x0, y0, x1 - x0, y1 - y0), True))

    def _shift_components(self, comp_ids: list[int], dy: float, reason: str) -> None:
        ids_set = set(comp_ids)
        self.relocation_causes.append(reason + f"，整体平移 {r(dy)}px")
        for cid in comp_ids:
            self.pending_shifts[cid] += dy
        for nid, nv in self.nodes.items():
            if nv.component in ids_set and not nv.is_new:
                old = self.old_node_boxes[nid]
                total_dy = self.pending_shifts[nv.component]
                nv.box.y = old.y + total_dy
                # 每个旧节点只保留一条最终位移记录
                self.displacements = [d for d in self.displacements
                                      if d["node_id"] != nid]
                self.displacements.append({
                    "node_id": nid,
                    "from": {"x": r(old.x), "y": r(old.y)},
                    "to": {"x": r(nv.box.x), "y": r(nv.box.y)},
                    "dx": r(nv.box.x - old.x), "dy": r(nv.box.y - old.y),
                    "reason": reason,
                })

    def _apply_pending_shifts_to_old_edges(self) -> None:
        """旧 EdgeView 在 _route 中按 v1 JSON 重建后，统一施加刚体平移。"""
        if not self.pending_shifts:
            return
        for eid, old_e in self.old_edges.items():
            ev = self.edges.get(eid)
            if ev is None:
                continue
            comps = {self.nodes[ev.source].component,
                     self.nodes[ev.target].component}
            dy = sum(self.pending_shifts[c] for c in comps if c in self.pending_shifts)
            if dy:
                ev.points = [(x, y + dy) for x, y in ev.points]
                if ev.label_box is not None:
                    ev.label_box.box.y += dy

    def _is_horizontal_adjacent(self, src: NodeView, dst: NodeView) -> bool:
        """相邻层且 x 方向确实推进（层间通道在两者之间）时才走标准短折线；
        增量插入到组件下方的节点虽然层号相邻，但沿 y 排列，必须走外走廊。"""
        if dst.rank != src.rank + 1:
            return False
        x1 = self.rank_x[(src.component, src.rank)]
        x2 = self.rank_x[(dst.component, dst.rank)]
        return x2 > x1 + 40

    # -- 路由：旧边原样恢复，新边走统一路由器 -------------------------------
    def _route(self) -> None:
        lane_idx, parallel_total = self._parallel_groups()
        channel_lanes = self._channel_lanes()
        for e in self.g.edges:
            li = lane_idx[e.edge_id]
            pcount = parallel_total[e.edge_id]
            sym = 0 if li == 0 else ((li + 1) // 2) * (1 if li % 2 else -1)
            ch_lane = channel_lanes.get(e.edge_id, 0)
            if e.edge_id in self.old_edges:
                old_e = self.old_edges[e.edge_id]
                lb = None
                if old_e.get("label_box"):
                    lb = LabelBox(old_e["label_box"]["text"],
                                  Box(**old_e["label_box"]["box"]))
                self.edges[e.edge_id] = EdgeView(
                    edge_id=e.edge_id, source=e.source, target=e.target,
                    label=e.label,
                    points=[tuple(p) for p in old_e["points"]],
                    is_self_loop=old_e["is_self_loop"],
                    is_parallel=old_e["is_parallel"],
                    lane_index=old_e["lane_index"],
                    parallel_count=old_e["parallel_count"],
                    kind=old_e["kind"], label_box=lb, is_new=False)
                continue
            src, dst = self.nodes[e.source], self.nodes[e.target]
            if e.source == e.target:
                pts, kind = self._route_self_loop(src, li, pcount), "self_loop"
            elif e.edge_id in self.a.back_edges:
                pts, kind = self._route_back_edge(src, dst, ch_lane), "backward"
            elif src.scc_id == dst.scc_id and src.in_cycle:
                pts, kind = self._route_scc_forward(src, dst, ch_lane), "forward"
            elif self._is_horizontal_adjacent(src, dst):
                pts, kind = self._route_adjacent(src, dst, sym, ch_lane), "forward"
            else:
                pts, kind = self._route_outer_corridor(src, dst, ch_lane), "forward"
            self.edges[e.edge_id] = EdgeView(
                edge_id=e.edge_id, source=e.source, target=e.target,
                label=e.label, points=pts,
                is_self_loop=(e.source == e.target),
                is_parallel=(pcount > 1), lane_index=li,
                parallel_count=pcount, kind=kind, is_new=True)

    def _place_labels(self) -> None:
        # 已有标签占位（旧边标签不可被新标签压住）
        occupied = [ev.label_box.box for ev in self.edges.values()
                    if ev.label_box is not None]
        for eid, ev in self.edges.items():
            if not ev.is_new or not ev.label:
                continue
            w = edge_label_width(ev.label)
            h = self.plan.label_box_height
            candidates = self._label_candidates(ev, w, h)
            for cand in candidates:
                lb = Box(cand[0], cand[1], w, h)
                if self._label_ok(lb, ev, occupied):
                    ev.label_box = LabelBox(ev.label, lb)
                    occupied.append(lb)
                    break
            else:
                lb = Box(candidates[0][0], candidates[0][1], w, h)
                ev.label_box = LabelBox(ev.label, lb)
                occupied.append(lb)
                self.issues.append(Issue(
                    "warning", "edge-label-conflict",
                    f"新边 {eid} 标签 '{ev.label}' 重试后仍冲突，已标注待人工复核",
                    [eid], {"at": {"x": r(lb.x), "y": r(lb.y)}}))

    def _rebuild_views_after_shift(self) -> None:
        all_comp_nodes: dict[int, list[str]] = defaultdict(list)
        for nid, nv in self.nodes.items():
            all_comp_nodes[nv.component].append(nid)
        self.comp_views = []
        for cid in sorted(all_comp_nodes):
            ids = all_comp_nodes[cid]
            boxes = [self.nodes[i].box for i in ids]
            self.comp_views.append(ComponentView(
                cid, sorted(ids),
                Box(min(b.x for b in boxes), min(b.y for b in boxes),
                    max(b.x2 for b in boxes) - min(b.x for b in boxes),
                    max(b.y2 for b in boxes) - min(b.y for b in boxes))))
        self._rebuild_scc_views()
        self.canvas_right = max(c.box.x2 for c in self.comp_views) + 30
        self.canvas_bottom = max(c.box.y2 for c in self.comp_views) + 30

    def build_update_report(self) -> dict:
        kept = sorted(set(self.old_node_boxes) - self.new_nodes)
        unchanged = [nid for nid in kept
                     if not any(d["node_id"] == nid for d in self.displacements)]
        return {
            "old_layout_node_count": len(self.old_node_boxes),
            "added_nodes": sorted(self.new_nodes),
            "added_edges": sorted(self.new_edges),
            "removed_nodes": sorted(set(self.old_node_boxes) -
                                    set(self.nodes)),
            "removed_edges": sorted(set(self.old_edges) - set(self.edges)),
            "frozen_node_count": len(unchanged),
            "moved_nodes": self.displacements,
            "relocation_causes": self.relocation_causes,
            "old_edges_reused": sorted(set(self.old_edges) & set(self.edges)),
        }

    def run(self) -> Layout:
        self._prepare_sizes()
        self._place()
        self._route()
        self._apply_pending_shifts_to_old_edges()
        # 平移后重建组件盒与 SCC 簇框，保证 JSON/SVG 几何与实际位置一致
        self._rebuild_views_after_shift()
        self._place_labels()
        self._audit_routes()
        canvas = Box(0, 0, r(self.canvas_right), r(self.canvas_bottom))
        layout = Layout(
            canvas=canvas, nodes=self.nodes, edges=self.edges,
            components=self.comp_views, sccs=self.scc_views,
            issues=self.issues, rank_x={},
            update=self.build_update_report())
        # 稳定性自检：未报告位移的旧节点必须与 v1 坐标逐点相同
        moved = {d["node_id"] for d in self.displacements}
        for nid, old_box in self.old_node_boxes.items():
            if nid in moved:
                continue
            nb = self.nodes[nid].box
            if (abs(nb.x - old_box.x) > 0.01 or abs(nb.y - old_box.y) > 0.01 or
                    abs(nb.width - old_box.width) > 0.01 or
                    abs(nb.height - old_box.height) > 0.01):
                layout.issues.append(Issue(
                    "error", "unreported-displacement",
                    f"旧节点 {nid} 坐标发生变化但未出现在位移报告中", [nid]))
        return layout
