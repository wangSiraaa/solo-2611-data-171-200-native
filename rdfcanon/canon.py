"""规范化: 关系邻域精化 + 个体化-精化 (individualization-refinement) 搜索。

算法分两段:

1. 颜色精化 (1-WL 风格, 多轮直至不动点):
   每个空白节点的签名 = (当前颜色, 排序后的"邻接描述符"多重集)。
   一条四元组给节点 b 的描述符记录 b 在四元组中的槽位、其余槽位的
   基项身份 (IRI / literal / 默认图) 或其他空白节点的当前颜色。
   签名只依赖基项与结构, 不依赖原标签与语句顺序。
   由于签名包含自身颜色, 划分只会变细, 至多 n 轮收敛。

2. 剩余对称: 若精化后仍有同颜色单元 (非离散划分), 取颜色最小的
   非单点单元, 对其中每个节点依次赋予新的最小颜色 (个体化), 重新
   精化后递归。叶子 (离散着色) 给出一种规范编号, 序列化之;
   所有叶子取字节最小者为规范形式。平局 (自同构) 时按
   (原标签, 规范编号) 字典序取最小, 保证同一输入文件结果稳定。

搜索状态 = 当前颜色向量 (一个划分) + 个体化路径; 每层递归至少使一个
单元分裂, 深度 <= n, 分支因子 <= 当前单元大小。n <= 8 时代价可忽略。

重命名不变性: 初始颜色全相同, 签名与序列化只含基项与结构位置,
个体化对单元内所有节点对称枚举并取全局最小, 因此重命名/换序只置换
内部下标, 候选序列化集合不变, 最小值不变。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from . import model
from .model import Dataset, Quad


@dataclass
class CanonResult:
    canonical_nquads: str           # 规范 N-Quads 文本 (UTF-8)
    sha256: str                     # 规范字节的 SHA256 (hex)
    labeling: dict[str, str]        # 原空白标签 -> 规范标签 ("c0" ...)
    stats: dict = field(default_factory=dict)

    @property
    def canonical_bytes(self) -> bytes:
        return self.canonical_nquads.encode("utf-8")


def _partition_key(colors: list) -> tuple:
    cells: dict = {}
    for node, color in enumerate(colors):
        cells.setdefault(color, []).append(node)
    return tuple(sorted(tuple(sorted(members)) for members in cells.values()))


class _Canonicalizer:
    def __init__(self, dataset: Dataset):
        self.dataset = dataset
        self.labels = dataset.bnode_labels          # 排序后的原标签
        self.index = {label: i for i, label in enumerate(self.labels)}
        self.n = len(self.labels)
        # 排序仅为计算过程确定; 结果由最小化保证与顺序无关。
        # None 归一成 "" 使异种 literal 可比较。
        def sortable(term):
            return tuple("" if x is None else x for x in term)

        self.quads: list[Quad] = sorted(
            dataset.quads, key=lambda q: tuple(sortable(t) for t in q)
        )
        # 每槽位预处理: 基项 -> 结构键; 空白节点 -> 内部下标
        # slot_key[qi][si] = ("g", ground_key) | ("b", node_index)
        self.slot_keys: list[list[tuple]] = []
        # 邻接表: node -> [(quad_index, quad)], quad 内该节点出现位置在描述时判定
        self.adj: list[list[int]] = [[] for _ in range(self.n)]
        for qi, quad in enumerate(self.quads):
            keys = []
            for term in quad:
                if model.is_bnode(term):
                    idx = self.index[term[1]]
                    keys.append(("b", idx))
                    if qi not in self.adj[idx]:
                        self.adj[idx].append(qi)
                else:
                    keys.append(("g", self._ground_key(term)))
            self.slot_keys.append(keys)
        # 搜索结果
        self.best_ser: str | None = None
        self.best_labeling: tuple[int, ...] | None = None
        self._line_cache: dict = {}
        self.stats = {
            "blank_nodes": self.n,
            "quads": len(self.quads),
            "refinement_rounds": 0,
            "cells_after_refinement": 0,
            "search_nodes": 0,
            "search_leaves": 0,
        }

    @staticmethod
    def _ground_key(term: tuple) -> tuple:
        kind = term[0]
        if kind == model.KIND_IRI:
            return ("i", term[1])
        if kind == model.KIND_LIT:
            # None 归一成 "" 以便可比较; datatype 为 None 当且仅当有语言标签
            return ("l", term[1], term[2] or "", term[3] or "")
        if kind == model.KIND_DEFAULT:
            return ("d",)
        raise ValueError(term)

    # ---------------- 精化 ----------------

    def _signature(self, node: int, colors: list[int]) -> tuple:
        descriptors = []
        for qi in self.adj[node]:
            slots = self.slot_keys[qi]
            desc = []
            for kind, val in slots:
                if kind == "g":
                    desc.append((2, val))
                elif val == node:
                    desc.append((0,))          # 自身出现的槽位
                else:
                    desc.append((1, colors[val]))  # 其他空白节点: 当前颜色
            descriptors.append(tuple(desc))
        descriptors.sort()
        return (colors[node], tuple(descriptors))

    def _refine(self, colors: list) -> list[int]:
        part = _partition_key(colors)
        while True:
            self.stats["refinement_rounds"] += 1
            sigs = [self._signature(b, colors) for b in range(self.n)]
            rank = {sig: i for i, sig in enumerate(sorted(set(sigs)))}
            new_colors = [rank[sig] for sig in sigs]
            new_part = _partition_key(new_colors)
            if new_part == part:
                return new_colors
            part = new_part
            colors = new_colors

    # ---------------- 序列化 ----------------

    def _serialize(self, labeling: tuple[int, ...]) -> str:
        names = ["c%d" % labeling[i] for i in range(self.n)]
        name_of = dict(zip(self.labels, names))
        lines = []
        for quad in self.quads:
            # 同一 (四元组, 涉及空白节点的规范编号) 的行文本跨叶子复用
            key = (quad, tuple(labeling[self.index[t[1]]] for t in quad if model.is_bnode(t)))
            line = self._line_cache.get(key)
            if line is None:
                line = model.quad_line(quad, name_of.get)
                self._line_cache[key] = line
            lines.append(line)
        lines.sort(key=lambda s: s.encode("utf-8"))
        return "".join(line + "\n" for line in lines)

    # ---------------- 搜索 ----------------

    def _labeling_key(self, labeling: tuple[int, ...]) -> tuple:
        # 平局裁定: 按 (原标签, 规范编号) 字典序最小; 只影响映射选取
        return tuple(sorted((self.labels[i], labeling[i]) for i in range(self.n)))

    def _search(self, colors: list[int]) -> None:
        self.stats["search_nodes"] += 1
        cells: dict[int, list[int]] = {}
        for node, color in enumerate(colors):
            cells.setdefault(color, []).append(node)
        ordered = [cells[c] for c in sorted(cells)]
        if all(len(cell) == 1 for cell in ordered):
            self.stats["search_leaves"] += 1
            order = [cell[0] for cell in ordered]
            labeling = [0] * self.n
            for canon_idx, node in enumerate(order):
                labeling[node] = canon_idx
            labeling = tuple(labeling)
            ser = self._serialize(labeling)
            if (
                self.best_ser is None
                or ser < self.best_ser
                or (ser == self.best_ser and self._labeling_key(labeling) < self._labeling_key(self.best_labeling))
            ):
                self.best_ser = ser
                self.best_labeling = labeling
            return
        # 分支规则: 颜色最小的非单点单元; 对单元内每个节点个体化
        cell = next(cell for cell in ordered if len(cell) > 1)
        for node in sorted(cell):
            c = colors[node]
            new_colors = [
                (c, 0) if i == node else (c, 1) if colors[i] == c else (colors[i], 2)
                for i in range(self.n)
            ]
            self._search(self._refine(new_colors))

    def run(self) -> CanonResult:
        if self.n == 0:
            ser = self._serialize(())
            digest = hashlib.sha256(ser.encode("utf-8")).hexdigest()
            return CanonResult(ser, digest, {}, {**self.stats, "search_leaves": 1, "search_nodes": 1})
        colors = self._refine([0] * self.n)
        self.stats["cells_after_refinement"] = len(set(colors))
        self._search(colors)
        assert self.best_ser is not None and self.best_labeling is not None
        digest = hashlib.sha256(self.best_ser.encode("utf-8")).hexdigest()
        labeling = {self.labels[i]: "c%d" % self.best_labeling[i] for i in range(self.n)}
        return CanonResult(self.best_ser, digest, labeling, dict(self.stats))


def canonicalize(dataset: Dataset) -> CanonResult:
    """计算 dataset 的规范形式。要求空白节点数 <= 8 (plan.json)。"""
    if len(dataset.bnode_labels) > 8:
        raise ValueError("more than 8 blank nodes; out of plan.json scope")
    return _Canonicalizer(dataset).run()
