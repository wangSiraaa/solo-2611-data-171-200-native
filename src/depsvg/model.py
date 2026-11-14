"""核心数据模型与图结构。

只使用标准库。所有节点以 node_id、所有边以 edge_id 作为稳定身份，
布局输出的每个图形元素都能回溯到这些原始 ID。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# 输入模型
# ---------------------------------------------------------------------------
@dataclass
class InputNode:
    node_id: str
    label: str
    width: float
    height: float


@dataclass
class InputEdge:
    edge_id: str
    source: str
    target: str
    label: str


@dataclass
class DisplayPlan:
    node_padding: float = 18.0
    route_clearance: float = 8.0
    label_box_height: float = 18.0
    font: str = "sans-serif"


# ---------------------------------------------------------------------------
# 布局后的几何模型（同时用于 JSON / SVG / verify，杜绝两套坐标）
# ---------------------------------------------------------------------------
@dataclass
class Box:
    """轴对齐矩形，坐标为左上角 (x, y)。"""
    x: float
    y: float
    width: float
    height: float

    @property
    def x2(self) -> float:
        return self.x + self.width

    @property
    def y2(self) -> float:
        return self.y + self.height

    @property
    def cx(self) -> float:
        return self.x + self.width / 2

    @property
    def cy(self) -> float:
        return self.y + self.height / 2

    def to_dict(self) -> dict:
        return {"x": r(self.x), "y": r(self.y),
                "width": r(self.width), "height": r(self.height)}


@dataclass
class NodeView:
    node_id: str
    label: str
    box: Box
    lines: list[str]                      # 换行后的标签行
    box_enlarged: bool = False            # 给定盒子是否因标签放不下而放大
    component: int = 0
    rank: int = 0
    scc_id: int = -1
    in_cycle: bool = False
    is_new: bool = False

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "label": self.label,
            "lines": self.lines,
            "box": self.box.to_dict(),
            "box_enlarged": self.box_enlarged,
            "component": self.component,
            "rank": self.rank,
            "scc_id": self.scc_id,
            "in_cycle": self.in_cycle,
            "is_new": self.is_new,
        }


@dataclass
class LabelBox:
    text: str
    box: Box

    def to_dict(self) -> dict:
        return {"text": self.text, "box": self.box.to_dict()}


@dataclass
class EdgeView:
    edge_id: str
    source: str
    target: str
    label: str
    points: list[tuple[float, float]]     # 折线点（含起止端口点）
    is_self_loop: bool
    is_parallel: bool                     # 与别的边共享同一对端点
    lane_index: int                       # 并行车道编号（0 起）
    parallel_count: int
    kind: str                             # forward / backward / self_loop
    label_box: Optional[LabelBox] = None
    is_new: bool = False

    def to_dict(self) -> dict:
        return {
            "edge_id": self.edge_id,
            "source": self.source,
            "target": self.target,
            "label": self.label,
            "kind": self.kind,
            "is_self_loop": self.is_self_loop,
            "is_parallel": self.is_parallel,
            "lane_index": self.lane_index,
            "parallel_count": self.parallel_count,
            "points": [[r(x), r(y)] for x, y in self.points],
            "label_box": self.label_box.to_dict() if self.label_box else None,
            "is_new": self.is_new,
        }


@dataclass
class SCCView:
    scc_id: int
    node_ids: list[str]
    box: Box
    is_cyclic: bool

    def to_dict(self) -> dict:
        return {"scc_id": self.scc_id, "node_ids": self.node_ids,
                "cyclic": self.is_cyclic, "box": self.box.to_dict()}


@dataclass
class ComponentView:
    component: int
    node_ids: list[str]
    box: Box

    def to_dict(self) -> dict:
        return {"component": self.component,
                "node_ids": self.node_ids, "box": self.box.to_dict()}


@dataclass
class Issue:
    severity: str       # error / warning / info
    code: str
    message: str
    ids: list[str] = field(default_factory=list)
    at: Optional[dict] = None

    def to_dict(self) -> dict:
        d = {"severity": self.severity, "code": self.code,
             "message": self.message, "ids": self.ids}
        if self.at is not None:
            d["at"] = self.at
        return d


@dataclass
class Layout:
    canvas: Box
    nodes: dict[str, NodeView]
    edges: dict[str, EdgeView]
    components: list[ComponentView]
    sccs: list[SCCView]
    issues: list[Issue]
    rank_x: dict[int, float]
    update: Optional[dict] = None
    source_files: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "schema": "depsvg-layout/1",
            "coordinate_unit": "SVG CSS px",
            "canvas": self.canvas.to_dict(),
            "rank_center_x": {str(k): r(v) for k, v in sorted(self.rank_x.items())},
            "components": [c.to_dict() for c in self.components],
            "sccs": [s.to_dict() for s in self.sccs],
            "nodes": [self.nodes[k].to_dict()
                      for k in sorted(self.nodes, key=lambda i: self.nodes[i].rank * 1000 + self.nodes[i].box.y)],
            "edges": [self.edges[k].to_dict() for k in sorted(self.edges)],
            "issues": [i.to_dict() for i in self.issues],
            "update": self.update,
            "source_files": self.source_files,
        }


def r(v: float) -> float:
    """坐标统一保留两位小数，SVG / JSON 完全一致。"""
    return round(float(v) + 1e-9, 2)


# ---------------------------------------------------------------------------
# 输入读取
# ---------------------------------------------------------------------------
def load_nodes(path: str) -> list[InputNode]:
    import json
    raw = json.load(open(path, encoding="utf-8"))
    out = []
    for item in raw:
        out.append(InputNode(item["node_id"], item["label"],
                             float(item["width"]), float(item["height"])))
    return out


def load_edges(path: str) -> list[InputEdge]:
    import csv
    out = []
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            out.append(InputEdge(row["edge_id"], row["source"],
                                 row["target"], row.get("label", "")))
    return out


def load_plan(path: str) -> DisplayPlan:
    import json
    p = json.load(open(path, encoding="utf-8"))
    lb = p.get("edge_label_box", {})
    return DisplayPlan(
        node_padding=float(p.get("node_padding", 18)),
        route_clearance=float(p.get("route_clearance", 8)),
        label_box_height=float(lb.get("height", 18)),
        font=p.get("font", "sans-serif"),
    )


class Graph:
    """邻接表图，附带弱连通分量与 Tarjan 强连通分量。"""

    def __init__(self, nodes: list[InputNode], edges: list[InputEdge]):
        self.nodes = nodes
        self.edges = edges
        self.node_ids = [n.node_id for n in nodes]
        self.nmap = {n.node_id: n for n in nodes}
        self.adj: dict[str, list[str]] = {n: [] for n in self.node_ids}
        self.in_adj: dict[str, list[str]] = {n: [] for n in self.node_ids}
        for e in edges:
            self.adj[e.source].append(e.target)
            self.in_adj[e.target].append(e.source)

    # -- 弱连通分量（无向） ------------------------------------------------
    def weak_components(self) -> list[list[str]]:
        seen: set[str] = set()
        comps: list[list[str]] = []
        undirected: dict[str, list[str]] = {n: [] for n in self.node_ids}
        for e in self.edges:
            undirected[e.source].append(e.target)
            undirected[e.target].append(e.source)
        for start in self.node_ids:
            if start in seen:
                continue
            stack = [start]
            comp: list[str] = []
            while stack:
                u = stack.pop()
                if u in seen:
                    continue
                seen.add(u)
                comp.append(u)
                stack.extend(v for v in undirected[u] if v not in seen)
            comps.append(sorted(comp))
        comps.sort(key=lambda c: (-len(c), c[0]))
        return comps

    # -- Tarjan 强连通分量 -------------------------------------------------
    def tarjan(self) -> list[list[str]]:
        index_counter = [0]
        stack: list[str] = []
        on_stack: set[str] = set()
        indices: dict[str, int] = {}
        low: dict[str, int] = {}
        result: list[list[str]] = []

        def strong(v: str) -> None:
            indices[v] = index_counter[0]
            low[v] = index_counter[0]
            index_counter[0] += 1
            stack.append(v)
            on_stack.add(v)
            for w in self.adj[v]:
                if w not in indices:
                    strong(w)
                    low[v] = min(low[v], low[w])
                elif w in on_stack:
                    low[v] = min(low[v], indices[w])
            if low[v] == indices[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                result.append(sorted(comp))

        import sys
        sys.setrecursionlimit(10000)
        for v in self.node_ids:
            if v not in indices:
                strong(v)
        return result
