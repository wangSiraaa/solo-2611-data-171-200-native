"""数据集判等: 依据规范字节判等, 并给出空白节点对应与语句来源映射。"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import model
from .canon import CanonResult, canonicalize
from .model import Dataset, Quad


@dataclass
class CompareResult:
    equivalent: bool
    canon_a: CanonResult
    canon_b: CanonResult
    # 等价时:
    bnode_mapping: dict[str, str] = field(default_factory=dict)  # A 原标签 -> B 原标签
    statements_a: list[dict] = field(default_factory=list)       # 每条原语句 -> 规范语句
    statements_b: list[dict] = field(default_factory=list)
    mapping_replay_ok: bool = False
    # 不等价时:
    only_in_a: list[str] = field(default_factory=list)           # 规范四元组集合差
    only_in_b: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {
            "equivalent": self.equivalent,
            "sha256_a": self.canon_a.sha256,
            "sha256_b": self.canon_b.sha256,
            "stats_a": self.canon_a.stats,
            "stats_b": self.canon_b.stats,
        }
        if self.equivalent:
            out["bnode_mapping_a_to_b"] = self.bnode_mapping
            out["statements_a"] = self.statements_a
            out["statements_b"] = self.statements_b
            out["verification"] = {"mapping_replay_ok": self.mapping_replay_ok}
        else:
            out["only_in_a"] = self.only_in_a
            out["only_in_b"] = self.only_in_b
            out["note"] = (
                "差异为两份规范形式的四元组集合差, 不声称是最小编辑, "
                "也不对实体相似性做任何猜测。"
            )
        return out


def _statement_provenance(ds: Dataset, canon: CanonResult) -> list[dict]:
    def canon_name(label: str) -> str:
        return canon.labeling[label]

    rows = []
    for line_no, raw, quad in ds.statements:
        rows.append(
            {
                "line": line_no,
                "statement": raw,
                "canonical": model.quad_line(quad, canon_name),
            }
        )
    return rows


def _apply_mapping(quads: set[Quad], mapping: dict[str, str]) -> set[Quad]:
    def map_term(term):
        if model.is_bnode(term):
            return model.bnode(mapping[term[1]])
        return term

    return {tuple(map_term(t) for t in quad) for quad in quads}


def compare(ds_a: Dataset, ds_b: Dataset) -> CompareResult:
    """按规范内容判等。等价时给出空白节点对应并逐四元组重放核验。"""
    canon_a = canonicalize(ds_a)
    canon_b = canonicalize(ds_b)
    result = CompareResult(
        equivalent=canon_a.canonical_bytes == canon_b.canonical_bytes,
        canon_a=canon_a,
        canon_b=canon_b,
    )
    if result.equivalent:
        # 同一规范编号 <-> 同一规范编号
        inv_b = {canon: orig for orig, canon in canon_b.labeling.items()}
        result.bnode_mapping = {
            orig_a: inv_b[canon] for orig_a, canon in canon_a.labeling.items()
        }
        result.statements_a = _statement_provenance(ds_a, canon_a)
        result.statements_b = _statement_provenance(ds_b, canon_b)
        # 逐四元组重放: 映射作用于 A 的四元组集合必须精确等于 B 的集合
        replayed = _apply_mapping(ds_a.quads, result.bnode_mapping)
        result.mapping_replay_ok = replayed == ds_b.quads
    else:
        lines_a = set(canon_a.canonical_nquads.splitlines())
        lines_b = set(canon_b.canonical_nquads.splitlines())
        result.only_in_a = sorted(lines_a - lines_b)
        result.only_in_b = sorted(lines_b - lines_a)
    return result
