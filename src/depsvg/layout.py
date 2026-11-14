"""层次布局引擎（无外部布局引擎，全部为自研启发式）。

流程：
  1. Tarjan 强连通分量 -> 缩点 DAG -> 最长路径分层（回边保留，不从图中删除）
  2. 弱连通分量分离，垂直堆叠，互不重叠
  3. 给定盒尺寸 + 确定性字符估算做标签换行，盒只能放大
  4. 层内垂直堆叠、层间水平排列；SCC 用虚线簇框圈出
  5. 边按前向/回边/自环/跨层长边分类，通道折线 + 并行车道偏移
  6. 边标签占位并与节点盒、其它标签做冲突检测，冲突即登记 issue
"""
from __future__ import annotations

from collections import defaultdict

from .model import (Box, ComponentView, DisplayPlan, EdgeView, Graph, Issue,
                    Layout, NodeView, SCCView, r)
from .text import (LINE_HEIGHT, edge_label_width, label_block_height,
                   needed_box_size)

# ---------------------------------------------------------------------------
# 布局常量（SVG px）
# ---------------------------------------------------------------------------
RANK_GAP = 120.0          # 层间水平距离（留给折线通道与标签）
NODE_GAP = 26.0           # 同层节点垂直间距
COMP_GAP = 90.0           # 独立组件之间的垂直间距
MARGIN = 30.0             # 画布外边距
LANE_DX = 16.0            # 平行边通道横向错开
PORT_SPREAD = 9.0         # 平行边端口纵向错开
CLUSTER_PAD_X = 26.0      # SCC 簇框内边距
CLUSTER_PAD_Y = 22.0
CORRIDOR_CLEAR = 14.0     # 外侧走廊离节点/簇框的净距
LABEL_GAP = 4.0           # 边标签与折线的间距
MAX_ROUTE_TRIES = 12
V_PAD = 4.0               # 节点盒垂直留白（水平留白仍用 node_padding=18）


# ---------------------------------------------------------------------------
# 1. 图分析：SCC、缩点 DAG、分层
# ---------------------------------------------------------------------------
class Analysis:
    def __init__(self, graph: Graph):
        self.g = graph
        self.raw_sccs = graph.tarjan()
        self.node_scc: dict[str, int] = {}
        self.scc_nodes: list[list[str]] = []
        self.cyclic: list[bool] = []
        for idx, members in enumerate(self.raw_sccs):
            self.scc_nodes.append(members)
            self.cyclic.append(len(members) > 1 or
                               any(e.source == e.target and e.source == members[0]
                                   for e in graph.edges))
            for m in members:
                self.node_scc[m] = idx
        self.scc_count = len(self.scc_nodes)

        # 缩点 DAG（去重，保留原始边映射）
        self.dag: dict[int, set[int]] = defaultdict(set)
        self.scc_edges: dict[tuple[int, int], list[str]] = defaultdict(list)
        for e in graph.edges:
            a, b = self.node_scc[e.source], self.node_scc[e.target]
            if a != b:
                self.dag[a].add(b)
                self.scc_edges[(a, b)].append(e.edge_id)

        # 弱连通分量
        weak = graph.weak_components()
        self.node_component: dict[str, int] = {}
        self.comp_nodes: list[list[str]] = []
        for cid, members in enumerate(weak):
            self.comp_nodes.append(members)
            for m in members:
                self.node_component[m] = cid

        # 每个组件内部独立做最长路径分层（层号在组件内从 0 开始）
        self.node_rank: dict[str, int] = {}
        for members in self.comp_nodes:
            comp_sccs = {self.node_scc[m] for m in members}
            rank: dict[int, int] = {s: 0 for s in comp_sccs}
            # DAG 最长路径：迭代松弛，规模 <=25 无需复杂拓扑缓存
            changed = True
            while changed:
                changed = False
                for a in comp_sccs:
                    ra = rank[a]
                    for b in self.dag[a]:
                        if b in comp_sccs and rank[b] < ra + 1:
                            rank[b] = ra + 1
                            changed = True
            for m in members:
                self.node_rank[m] = rank[self.node_scc[m]]

        # 回边识别：在每个 SCC 内部做 DFS，DFS 树/前向边保持前向（层号不
        # 倒退），只有指向祖先的 back edge 才走外侧回边通道——环保留但不会
        # 把同 SCC 的正常边都画成回边。
        self.back_edges: set[str] = set()
        edge_by_pair: dict[tuple[str, str], list[str]] = defaultdict(list)
        for e in graph.edges:
            edge_by_pair[(e.source, e.target)].append(e.edge_id)
        for sid, members in enumerate(self.scc_nodes):
            if not self.cyclic[sid]:
                continue
            mset = set(members)
            color: dict[str, int] = {m: 0 for m in members}   # 0 白 1 灰 2 黑

            def dfs(u: str) -> None:
                color[u] = 1
                for v in self.g.adj[u]:
                    if v not in mset:
                        continue
                    if color[v] == 0:
                        dfs(v)
                    elif color[v] == 1:
                        # u -> 祖先 v：回边（平行边共享同一定性，取全部）
                        for eid in edge_by_pair[(u, v)]:
                            self.back_edges.add(eid)
                color[u] = 2

            for m in sorted(members):
                if color[m] == 0:
                    dfs(m)

    def ranks_of_component(self, cid: int) -> list[int]:
        rks = sorted({self.node_rank[m] for m in self.comp_nodes[cid]})
        return rks


# ---------------------------------------------------------------------------
# 2. 主布局
# ---------------------------------------------------------------------------
class Engine:
    def __init__(self, graph: Graph, plan: DisplayPlan,
                 new_node_ids: set[str] | None = None,
                 new_edge_ids: set[str] | None = None):
        self.g = graph
        self.plan = plan
        self.a = Analysis(graph)
        self.nodes: dict[str, NodeView] = {}
        self.edges: dict[str, EdgeView] = {}
        self.scc_views: list[SCCView] = []
        self.comp_views: list[ComponentView] = []
        self.issues: list[Issue] = []
        self.rank_x: dict[tuple[int, int], float] = {}     # (comp, rank) -> 中心 x
        self.new_nodes = new_node_ids or set()
        self.new_edges = new_edge_ids or set()

    # -- 2a. 节点盒尺寸（含标签换行/放大） ---------------------------------
    def _prepare_sizes(self) -> None:
        for n in self.g.nodes:
            w, h, lines, enlarged = needed_box_size(
                n.label, n.width, n.height, self.plan.node_padding)
            box = Box(0, 0, w, h)
            nv = NodeView(
                node_id=n.node_id, label=n.label, box=box, lines=lines,
                box_enlarged=enlarged,
                component=self.a.node_component[n.node_id],
                rank=self.a.node_rank[n.node_id],
                scc_id=self.a.node_scc[n.node_id],
                in_cycle=self.a.cyclic[self.a.node_scc[n.node_id]],
                is_new=n.node_id in self.new_nodes)
            self.nodes[n.node_id] = nv
            if enlarged:
                self.issues.append(Issue(
                    "info", "node-box-enlarged",
                    f"节点 {n.node_id} 给定盒放不下换行标签，已按 display-plan "
                    f"放大至 {r(w)}x{r(h)}（原 {r(n.width)}x{r(n.height)}）",
                    [n.node_id]))

    # -- 2b. 组件/层/节点坐标 ----------------------------------------------
    def _place(self) -> None:
        cur_y = MARGIN
        comp_boxes: list[Box] = []
        for cid, members in enumerate(self.a.comp_nodes):
            ranks = self.a.ranks_of_component(cid)
            # 每层节点（保持输入顺序，确定性）
            rank_nodes: dict[int, list[NodeView]] = defaultdict(list)
            for nid in members:
                rank_nodes[self.a.node_rank[nid]].append(self.nodes[nid])

            # SCC 块需要额外的簇框上下边距
            scc_extra: dict[int, float] = defaultdict(float)
            for sid, ms in enumerate(self.a.scc_nodes):
                if not self.a.cyclic[sid]:
                    continue
                comp = self.a.node_component[ms[0]]
                if comp != cid:
                    continue
                rks = [self.a.node_rank[m] for m in ms]
                scc_extra[min(rks)] += CLUSTER_PAD_Y
                scc_extra[max(rks)] += CLUSTER_PAD_Y

            # 垂直方向：自上而下堆叠各层
            rank_top: dict[int, float] = {}
            y = cur_y
            for rk in ranks:
                y += scc_extra.get(rk, 0)
                rank_top[rk] = y
                block_h = sum(n.box.height for n in rank_nodes[rk]) + \
                    NODE_GAP * (len(rank_nodes[rk]) - 1)
                y += block_h + scc_extra.get(rk, 0) + RANK_GAP
            comp_bottom = y - RANK_GAP

            # 水平方向：每层一个 x 中心，层宽取该层最宽节点
            rank_w: dict[int, float] = {}
            for rk in ranks:
                rank_w[rk] = max(n.box.width for n in rank_nodes[rk])
            x = MARGIN
            rank_cx: dict[int, float] = {}
            for rk in ranks:
                cx = x + rank_w[rk] / 2
                rank_cx[rk] = cx
                self.rank_x[(cid, rk)] = cx
                x += rank_w[rk] + RANK_GAP
            comp_right = x - RANK_GAP

            # 落节点盒
            for rk in ranks:
                ny = rank_top[rk]
                for nv in rank_nodes[rk]:
                    nv.box.x = rank_cx[rk] - nv.box.width / 2
                    nv.box.y = ny
                    ny += nv.box.height + NODE_GAP

            cbox = Box(MARGIN, cur_y, comp_right - MARGIN,
                       comp_bottom - cur_y)
            comp_boxes.append(cbox)
            self.comp_views.append(ComponentView(cid, list(members), cbox))
            cur_y = comp_bottom + COMP_GAP

        self.canvas_bottom = cur_y - COMP_GAP + MARGIN
        self.canvas_right = max(c.x2 for c in comp_boxes) + MARGIN

        # SCC 簇框（圈住成员节点盒，回边在簇外绕行，环不会被视觉抹掉）
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

    # -- 2c. 并行边分组与通道路由 -------------------------------------------
    def _parallel_groups(self) -> tuple[dict[str, int], dict[str, int]]:
        groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for e in self.g.edges:
            groups[(e.source, e.target)].append(e.edge_id)
        lane: dict[str, int] = {}
        total: dict[str, int] = {}
        for key, ids in groups.items():
            ids.sort()
            for i, eid in enumerate(ids):
                lane[eid] = i
                total[eid] = len(ids)
        return lane, total

    def _channel_lanes(self) -> dict[str, int]:
        """同一通道路由的所有边（含平行边）各占一个车道，保证不共线。"""
        channels: dict[str, list[str]] = defaultdict(list)
        for e in self.g.edges:
            if e.source == e.target:
                continue
            src = self.nodes[e.source]
            key = self._channel_key(e, src)
            channels[key].append(e.edge_id)
        result: dict[str, int] = {}
        for key, eids in channels.items():
            for i, eid in enumerate(sorted(eids)):
                result[eid] = i
        return result

    def _is_horizontal_adjacent(self, src: NodeView, dst: NodeView) -> bool:
        """相邻层且沿 x 方向推进，标准层间短折线适用。"""
        return dst.rank == src.rank + 1

    def _channel_key(self, e, src: NodeView) -> str:
        cid = src.component
        if e.edge_id in self.a.back_edges:
            return f"back-right:{cid}"
        dst = self.nodes[e.target]
        if src.scc_id == dst.scc_id and src.in_cycle:
            return f"scc-left:{src.scc_id}"
        if self._is_horizontal_adjacent(src, dst):
            return f"mid:{cid}:{src.rank}"
        return f"outer-right:{cid}"

    # -- 2d. 边路由 ---------------------------------------------------------
    def _route(self) -> None:
        lane_idx, parallel_total = self._parallel_groups()
        channel_lanes = self._channel_lanes()
        for e in self.g.edges:
            src = self.nodes[e.source]
            dst = self.nodes[e.target]
            li = lane_idx[e.edge_id]
            pcount = parallel_total[e.edge_id]
            is_par = pcount > 1
            # 平行边的对称端口偏移：0, +1, -1, +2, -2 ...
            sym = 0 if li == 0 else ((li + 1) // 2) * (1 if li % 2 else -1)
            ch_lane = channel_lanes.get(e.edge_id, 0)

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

            ev = EdgeView(
                edge_id=e.edge_id, source=e.source, target=e.target,
                label=e.label, points=pts,
                is_self_loop=(e.source == e.target),
                is_parallel=is_par, lane_index=li, parallel_count=pcount,
                kind=kind, is_new=e.edge_id in self.new_edges)
            self.edges[e.edge_id] = ev

    def _port(self, nv: NodeView, side: str, offset: float = 0.0) -> tuple[float, float]:
        b = nv.box
        if side == "R":
            return (r(b.x2), r(b.cy + offset))
        if side == "L":
            return (r(b.x), r(b.cy + offset))
        if side == "T":
            return (r(b.cx + offset), r(b.y))
        return (r(b.cx + offset), r(b.y2))

    def _route_adjacent(self, src: NodeView, dst: NodeView, sym: int,
                        lane: int) -> list[tuple[float, float]]:
        """相邻层：层间通道垂直段，同通道每条边一个车道。"""
        cid = src.component
        x1 = self.rank_x[(cid, src.rank)]
        x2 = self.rank_x[(cid, dst.rank)]
        chan_x = r((x1 + x2) / 2 + lane * LANE_DX)
        p0 = self._port(src, "R", sym * PORT_SPREAD)
        p3 = self._port(dst, "L", sym * PORT_SPREAD)
        return [p0, (chan_x, p0[1]), (chan_x, p3[1]), p3]

    def _route_outer_corridor(self, src: NodeView, dst: NodeView,
                              lane: int) -> list[tuple[float, float]]:
        """跨层长边：组件最右外侧走廊，通道内逐车道错开。"""
        cid = src.component
        comp_box = self.comp_views[cid].box
        rightmost = max(self.nodes[m].box.x2 for m in self.a.comp_nodes[cid])
        corridor_x = r(max(comp_box.x2, rightmost) + CORRIDOR_CLEAR + lane * LANE_DX)
        p0 = self._port(src, "R")
        p3 = self._port(dst, "R")
        return [p0, (corridor_x, p0[1]), (corridor_x, p3[1]), p3]

    def _route_back_edge(self, src: NodeView, dst: NodeView,
                         lane: int) -> list[tuple[float, float]]:
        """环内回边：绕 SCC 簇框右侧外走廊，明确可见、不删边。"""
        sid = src.scc_id
        scc_box = next(s.box for s in self.scc_views if s.scc_id == sid)
        corridor_x = r(scc_box.x2 + CORRIDOR_CLEAR + lane * LANE_DX)
        p0 = self._port(src, "R")
        p3 = self._port(dst, "R")
        return [p0, (corridor_x, p0[1]), (corridor_x, p3[1]), p3]

    def _route_scc_forward(self, src: NodeView, dst: NodeView,
                           lane: int) -> list[tuple[float, float]]:
        """同层 SCC 内的前向边：绕簇框左侧走廊，与右侧回边区分。"""
        sid = src.scc_id
        scc_box = next(s.box for s in self.scc_views if s.scc_id == sid)
        corridor_x = r(scc_box.x - CORRIDOR_CLEAR - lane * LANE_DX)
        p0 = self._port(src, "L")
        p3 = self._port(dst, "L")
        return [p0, (corridor_x, p0[1]), (corridor_x, p3[1]), p3]

    def _route_self_loop(self, nv: NodeView, lane: int, total: int) -> list[tuple[float, float]]:
        """自环：节点上方显式 U 形折线；位于 SCC 簇框之上，并行自环逐层抬高。"""
        b = nv.box
        scc = next((s for s in self.scc_views if s.scc_id == nv.scc_id), None)
        base_top = scc.box.y if scc is not None else b.y
        # U 形最高点要高于簇框顶，留出标签与簇框标题的空间
        lift = max(26.0, b.y - base_top + 34.0) + lane * 18.0
        top = b.y - lift
        span = min(46.0, b.width * 0.4)
        p0 = (r(b.cx - span / 2), r(b.y))
        p3 = (r(b.cx + span / 2), r(b.y))
        return [p0, (p0[0], r(top)), (r(b.cx), r(top - 10)),
                (p3[0], r(top)), p3]

    # -- 2e. 边标签与冲突检测 ----------------------------------------------
    def _place_labels(self) -> None:
        occupied_labels: list[LabelBoxLike] = []
        for eid, ev in self.edges.items():
            if not ev.label:
                continue
            w = edge_label_width(ev.label)
            h = self.plan.label_box_height
            candidates = self._label_candidates(ev, w, h)
            placed = False
            for cand in candidates:
                lb = Box(cand[0], cand[1], w, h)
                if self._label_ok(lb, ev, occupied_labels):
                    from .model import LabelBox
                    ev.label_box = LabelBox(ev.label, lb)
                    occupied_labels.append(lb)
                    placed = True
                    break
            if not placed:
                self.issues.append(Issue(
                    "warning", "edge-label-conflict",
                    f"边 {eid} 的标签 '{ev.label}' 经 {MAX_ROUTE_TRIES} 次有界重试"
                    "仍与节点盒或其它标签冲突，已取首个候选位置并标注，请人工复核",
                    [eid],
                    {"at": {"x": r(candidates[0][0]), "y": r(candidates[0][1])}}))
                from .model import LabelBox
                lb = Box(candidates[0][0], candidates[0][1], w, h)
                ev.label_box = LabelBox(ev.label, lb)
                occupied_labels.append(lb)

    def _label_candidates(self, ev: EdgeView, w: float, h: float) -> list[tuple[float, float]]:
        """沿折线段中点两侧生成候选，按段长排序，有界重试。"""
        pts = ev.points
        segs = []
        for i in range(len(pts) - 1):
            p1, p2 = pts[i], pts[i + 1]
            length = abs(p2[0] - p1[0]) + abs(p2[1] - p1[1])
            segs.append((length, p1, p2))
        segs.sort(reverse=True)
        out: list[tuple[float, float]] = []
        seen: set[tuple[float, float]] = set()

        def add(cx: float, cy: float) -> None:
            cand = (r(cx - w / 2), r(cy - h / 2))
            if cand not in seen:
                seen.add(cand)
                out.append(cand)

        for length, p1, p2 in segs:
            mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
            horizontal = abs(p2[1] - p1[1]) < 0.5
            if ev.is_self_loop:
                # 自环标签固定在 U 形顶部上方
                add(mx, min(p1[1], p2[1]) - LABEL_GAP - h / 2)
                continue
            if horizontal:
                for shift in (-(LABEL_GAP + h / 2), LABEL_GAP + h / 2,
                              -2 * (LABEL_GAP + h), 2 * (LABEL_GAP + h)):
                    add(mx, my + shift)
            else:
                for shift in (LABEL_GAP + w / 2, -(LABEL_GAP + w / 2),
                              2 * (LABEL_GAP + w), -2 * (LABEL_GAP + w)):
                    add(mx + shift, my)
        return out[:MAX_ROUTE_TRIES]

    def _label_ok(self, lb: Box, ev: EdgeView, others: list[Box]) -> bool:
        from .text import boxes_overlap
        for nid, nv in self.nodes.items():
            if nid in (ev.source, ev.target):
                # 端点盒边界上允许贴合，但不能压进盒内
                if boxes_overlap(lb, nv.box):
                    return False
            elif boxes_overlap(lb, nv.box):
                return False
        for other in others:
            if boxes_overlap(lb, other):
                return False
        return True

    # -- 2f. 边-节点穿越检测（路由自检，跨边交叉允许并登记） ----------------
    def _audit_routes(self) -> None:
        from .text import segment_intersects_box
        for eid, ev in self.edges.items():
            for nid, nv in self.nodes.items():
                if ev.is_self_loop:
                    allowed = {ev.source}
                else:
                    allowed = {ev.source, ev.target}
                for i in range(len(ev.points) - 1):
                    p1, p2 = ev.points[i], ev.points[i + 1]
                    hits = segment_intersects_box(p1, p2, nv.box)
                    for pt in hits:
                        at_endpoint = any(
                            abs(pt[0] - q[0]) < 0.6 and abs(pt[1] - q[1]) < 0.6
                            for q in (ev.points[0], ev.points[-1]))
                        if nid not in allowed and not at_endpoint:
                            self.issues.append(Issue(
                                "error", "edge-crosses-node",
                                f"边 {eid} 折线穿过无关节点 {nid}，交点 ({pt[0]}, {pt[1]})",
                                [eid, nid], {"at": {"x": pt[0], "y": pt[1]}}))
                        elif nid in allowed and not at_endpoint:
                            self.issues.append(Issue(
                                "warning", "edge-grazes-own-node",
                                f"边 {eid} 折线进入端点节点 {nid} 盒内部（非端口点）",
                                [eid, nid], {"at": {"x": pt[0], "y": pt[1]}}))
        self._record_edge_crossings()

    def _record_edge_crossings(self) -> None:
        from .text import segments_intersect
        items = list(self.edges.items())
        reported: set[tuple[str, str, float, float]] = set()
        for i in range(len(items)):
            id_a, ea = items[i]
            for j in range(i + 1, len(items)):
                id_b, eb = items[j]
                shared = {ea.source, ea.target} & {eb.source, eb.target}
                for s in range(len(ea.points) - 1):
                    a1, a2 = ea.points[s], ea.points[s + 1]
                    for t in range(len(eb.points) - 1):
                        b1, b2 = eb.points[t], eb.points[t + 1]
                        ok, pt = segments_intersect(a1, a2, b1, b2)
                        if not ok or pt is None:
                            continue
                        # 共享端点处的连接不算“交叉”
                        endpoint_pts = set(ea.points[:1] + ea.points[-1:] +
                                           eb.points[:1] + eb.points[-1:])
                        if any(abs(pt[0] - q[0]) < 0.6 and abs(pt[1] - q[1]) < 0.6
                               for q in endpoint_pts) and shared:
                            continue
                        key = (id_a, id_b, pt[0], pt[1])
                        if key in reported:
                            continue
                        reported.add(key)
                        self.issues.append(Issue(
                            "info", "edge-crossing",
                            f"边 {id_a} 与 {id_b} 在 ({pt[0]}, {pt[1]}) 交叉"
                            "（允许，交叉点不渲染为节点）",
                            [id_a, id_b], {"at": {"x": pt[0], "y": pt[1]}}))

    def run(self) -> Layout:
        self._prepare_sizes()
        self._place()
        self._route()
        self._place_labels()
        self._audit_routes()
        canvas = Box(0, 0, r(self.canvas_right), r(self.canvas_bottom))
        # 层中心 x 用 (组件号*1000+层号) 整数键输出，增量模式据此复原
        rank_x_out = {cid * 1000 + rk: x for (cid, rk), x in self.rank_x.items()}
        return Layout(
            canvas=canvas, nodes=self.nodes, edges=self.edges,
            components=self.comp_views, sccs=self.scc_views,
            issues=self.issues, rank_x=rank_x_out)


# 类型别名避免循环导入
LabelBoxLike = Box
