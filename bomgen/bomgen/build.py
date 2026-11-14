"""构建编排: 展开 → 约束检查 → 汇总 → 自核对 → 规范化结果。

规范化(排序)保证: 交换定义输入顺序不改变规范结果。
"""
from __future__ import annotations

from typing import Any

from .constraints import evaluate_constraints
from .diag import Diag
from .expand import Expander, OccNode
from .model import Config, Model


def run_build(model: Model, config: Config, pre_diags: list[Diag] | None = None) -> dict:
    diags: list[Diag] = list(pre_diags or [])
    occurrences: list[dict] = []
    parts: list[dict] = []
    constraint_results: list[dict] = []

    root_def = model.definitions.get(config.root_definition)
    if root_def is None or root_def.kind != "assembly":
        if not any(d.code == "CONFIG_BAD_ROOT" for d in diags):
            diags.append(Diag("CONFIG_BAD_ROOT", "error",
                              f"根定义 {config.root_definition!r} 不存在或不是装配"))
    else:
        root, exp_diags = Expander(model, config).expand()
        diags.extend(exp_diags)
        constraint_results = evaluate_constraints(model, root, diags)
        occurrences = _flatten(root)
        parts = _aggregate(root)

    # 自核对: 每个汇总零件必须能从发生贡献重新累加得到
    for p in parts:
        if sum(c["qty"] for c in p["contributions"]) != p["total"]:
            diags.append(Diag("INTERNAL_INCONSISTENT", "error",
                              f"零件 {p['part']} 汇总与发生贡献不一致"))

    diags.sort(key=lambda d: d.sort_key())
    errors = sum(1 for d in diags if d.severity == "error")
    warnings = sum(1 for d in diags if d.severity == "warning")
    gaps = sum(1 for d in diags if d.code == "GAP_UNKNOWN_CONDITION")
    # false 与 unknown 都阻止声明完整可制造; 已确定的发生与物料仍导出并标为 partial
    status = "complete" if errors == 0 else "partial"
    cstat = {"satisfied": 0, "violated": 0, "pending": 0}
    for c in constraint_results:
        cstat[c["status"]] += 1

    return {
        "root_definition": config.root_definition,
        "root_id": config.root_id,
        "config": {
            "name": config.name,
            "options": dict(sorted(config.options.items())),
            "instance_overrides": {
                ".".join(p): dict(sorted(o.items()))
                for p, o in sorted(config.instance_overrides.items())
            },
        },
        "status": status,
        "releasable": status == "complete",
        "summary": {
            "occurrences": len(occurrences),
            "parts": len(parts),
            "errors": errors,
            "warnings": warnings,
            "gaps": gaps,
            "constraints": cstat,
        },
        "occurrences": occurrences,
        "parts": parts,
        "constraints": constraint_results,
        "diagnostics": [d.to_json() for d in diags],
    }


def _flatten(root: OccNode) -> list[dict]:
    out: list[dict] = []

    def rec(n: OccNode) -> None:
        e: dict[str, Any] = {
            "path": ".".join(n.path),
            "parent": ".".join(n.path[:-1]) or None,
            "id": n.id,
            "ref": n.ref,
            "kind": n.kind,
        }
        if n.kind == "part":
            e["unit"] = n.unit
        e["edge_qty"] = n.edge_qty
        e["qty"] = n.qty
        if n.edge_overrides:
            e["edge_overrides"] = dict(sorted(n.edge_overrides.items()))
        if n.inst_overrides:
            e["instance_overrides"] = dict(sorted(n.inst_overrides.items()))
        e["options"] = dict(sorted(n.scope.items()))
        out.append(e)
        for c in n.children:
            rec(c)

    rec(root)
    out.sort(key=lambda e: e["path"].split("."))
    return out


def _aggregate(root: OccNode) -> list[dict]:
    """同一零件汇总, 保留各发生贡献; 数量已在展开时沿路径相乘。"""
    acc: dict[str, dict] = {}

    def rec(n: OccNode) -> None:
        if n.kind == "part":
            e = acc.setdefault(n.ref, {"unit": n.unit, "total": 0, "contributions": []})
            e["total"] += n.qty
            e["contributions"].append({"path": ".".join(n.path), "qty": n.qty})
        for c in n.children:
            rec(c)

    rec(root)
    return [
        {"part": p, "unit": a["unit"], "total": a["total"],
         "contributions": sorted(a["contributions"], key=lambda c: c["path"].split("."))}
        for p, a in sorted(acc.items())
    ]


def verify_result(result: dict) -> list[str]:
    """独立核对: 从 occurrences 重新累加, 与 parts 汇总比对。返回问题列表(空=通过)。"""
    problems: list[str] = []
    by_part: dict[str, dict[str, Any]] = {}
    for o in result.get("occurrences", []):
        if o.get("kind") != "part":
            continue
        e = by_part.setdefault(o["ref"], {"unit": o["unit"], "contrib": {}})
        if o["unit"] != e["unit"]:
            problems.append(f"零件 {o['ref']} 单位不一致: {e['unit']} vs {o['unit']}")
        e["contrib"][o["path"]] = e["contrib"].get(o["path"], 0) + o["qty"]
    listed = {p["part"] for p in result.get("parts", [])}
    for ref, e in sorted(by_part.items()):
        if ref not in listed:
            problems.append(f"零件 {ref} 出现在发生中但缺少汇总行")
    for p in result.get("parts", []):
        contrib = {c["path"]: c["qty"] for c in p["contributions"]}
        if sum(contrib.values()) != p["total"]:
            problems.append(
                f"零件 {p['part']} 总量 {p['total']} != 贡献之和 {sum(contrib.values())}")
        occ = by_part.get(p["part"], {}).get("contrib")
        if occ is None:
            problems.append(f"零件 {p['part']} 有汇总行但无对应发生")
        elif occ != contrib:
            problems.append(f"零件 {p['part']} 的贡献与发生记录不一致: {occ} vs {contrib}")
        unit = by_part.get(p["part"], {}).get("unit")
        if unit is not None and unit != p["unit"]:
            problems.append(f"零件 {p['part']} 单位不一致: {unit} vs {p['unit']}")
    return problems
