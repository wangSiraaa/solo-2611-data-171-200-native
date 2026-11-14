"""结构化检查报告与 check / explain 公共入口。

check 负责装载文件、解析、编译、求解，并把结果整理成与输入文件顺序无关、
可 JSON 序列化的结构化报告；explain 在同一报告上生成中文人类可读说明。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

_TEMP_RE = re.compile(r"^t\d+$")


def _is_temp(name: str) -> bool:
    """临时节点变量严格形如 t0/t12；不能用 startswith，否则误伤 time 等实变量。"""
    return bool(_TEMP_RE.match(name))

from .compiler import CompiledSystem, Compiler, Item
from .diagnostics import DiagnosticBag, Severity
from .dimensions import BASES, format_dimension, format_fraction
from .dims import parse_dims_file
from .parser import parse_formula_file
from .solver import ComponentConflict, SolveResult, solve
from .sources import SourceStore, Span


@dataclass
class CheckResult:
    sources: SourceStore
    diagnostics: DiagnosticBag
    system: CompiledSystem
    solve: SolveResult
    variables: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    rejected_items: list[dict[str, Any]]
    status: str  # "consistent" | "inconsistent" | "parse_error_only"


def discover_files(paths: list[str]) -> list[str]:
    """目录递归展开为 .dims / .formula 文件；文件按路径排序保证可复现。"""
    found: list[str] = []
    for path in paths:
        if os.path.isdir(path):
            for root, _dirs, names in os.walk(path):
                for name in names:
                    if name.endswith((".dims", ".formula")):
                        found.append(os.path.join(root, name))
        else:
            found.append(path)
    return sorted(found)


def check(paths: list[str], sources: SourceStore | None = None) -> CheckResult:
    store = sources or SourceStore()
    diags = DiagnosticBag()
    compiler = Compiler(store, diags)

    for path in discover_files(paths):
        source = store.load(path)
        if path.endswith(".dims"):
            declarations = parse_dims_file(source, diags)
            compiler.compile(declarations, [], path)
        elif path.endswith(".formula"):
            formulas = parse_formula_file(source, diags)
            compiler.compile([], formulas, path)
        else:
            raise ValueError(f"不支持的文件类型（需要 .dims 或 .formula）: {path}")

    system = compiler.result()
    result = solve(system)
    variables = _build_variables(store, system, result)
    conflicts = _build_conflicts(store, system, result)
    rejected = _build_rejected(store, system)

    if diags.has_dim_conflict or not result.consistent:
        status = "inconsistent"
    elif diags.has_parse_error or any(
        d.severity in (Severity.PARSE_ERROR, Severity.SEMANTIC_ERROR) for d in diags.items
    ):
        status = "parse_error_only"
    else:
        status = "consistent"

    return CheckResult(
        sources=store,
        diagnostics=diags,
        system=system,
        solve=result,
        variables=variables,
        conflicts=conflicts,
        rejected_items=rejected,
        status=status,
    )


# --------------------------------------------------------------------- #
# 变量结果
# --------------------------------------------------------------------- #


def _span_dict(span: Span) -> dict[str, Any]:
    return {
        "file": span.file,
        "line": span.start_line,
        "column": span.start_col,
        "end_line": span.end_line,
        "end_column": span.end_col,
        "start_offset": span.start_offset,
        "end_offset": span.end_offset,
    }


def _build_variables(
    store: SourceStore, system: CompiledSystem, result: SolveResult
) -> list[dict[str, Any]]:
    if not result.consistent:
        # 矛盾系统不给貌似确定的单位；只登记变量与其来源。
        return [
            {
                "name": name,
                "status": "unresolved_conflict",
                "sources": [_span_dict(s) for s in system.variable_spans[name]],
            }
            for name in system.variables
            if not _is_temp(name)
        ]

    particular = result.particular
    nullspace = result.nullspace
    free_names = result.free_variables
    # 零空间的每个基向量统一编号为 k0,k1,...；其中仅含临时节点的基已在求解器
    # 剔除，因此这里每个参数都会在至少一个命名变量上出现，可对外展示。
    real_free = [n for n in free_names if not _is_temp(n)]
    hidden_free = [n for n in free_names if _is_temp(n)]
    param_index = {name: k for k, name in enumerate(free_names)}
    scalar_dof = 4 * len(free_names)

    output: list[dict[str, Any]] = []
    for name in system.variables:
        if _is_temp(name):
            continue
        vec = tuple(particular[name])
        basis_coeffs: list[tuple[str, tuple[Fraction, ...]]] = []
        for free_name, basis in zip(free_names, nullspace):
            coeff_vec = tuple(basis[name])
            if any(c != 0 for c in coeff_vec):
                basis_coeffs.append((f"k{param_index[free_name]}", coeff_vec))

        if not basis_coeffs:
            status = "declared" if _has_declaration(system, name) else "uniquely_inferred"
            param_repr = None
        else:
            status = "free"
            param_repr = _render_parametric(vec, basis_coeffs)

        output.append(
            {
                "name": name,
                "status": status,
                "dimension": [format_fraction(Fraction(c)) for c in vec],
                "dimension_text": format_dimension(vec),
                "parametric": param_repr,
                "depends_on_items": sorted(_dependencies(system, result, name)),
                "sources": [_span_dict(s) for s in system.variable_spans[name]],
            }
        )

    output.append(
        {
            "name": "__degrees_of_freedom__",
            "status": "meta",
            "free_dimension_variables": real_free,
            "internal_temp_free_variables": hidden_free,
            "nullspace_rank": len(free_names),
            "scalar_degrees_of_freedom": scalar_dof,
        }
    )
    return output


def _has_declaration(system: CompiledSystem, name: str) -> bool:
    return any(
        item.kind == "declaration" and item.name == name for item in system.items
    )


def _dependencies(system: CompiledSystem, result: SolveResult, name: str) -> set[str]:
    """通过约束连接关系收集结论所依赖的 statement（BFS 于约束超图）。"""
    if name in result.free_variables:
        return set()
    seen_items: set[str] = set()
    frontier = {name}
    visited_vars: set[str] = set()
    while frontier:
        current = frontier.pop()
        if current in visited_vars:
            continue
        visited_vars.add(current)
        for con in system.constraints:
            vars_here = {v for v, _ in con.terms}
            if current not in vars_here:
                continue
            if con.item_id in seen_items:
                continue
            seen_items.add(con.item_id)
            for other in vars_here:
                if other not in visited_vars:
                    frontier.add(other)
    return seen_items


def _render_parametric(
    particular: tuple[Fraction, ...],
    basis: list[tuple[str, tuple[Fraction, ...]]],
) -> dict[str, Any]:
    """dim(name) = particular + sum k_i * basis_i；分量逐项给出。"""
    components: dict[str, str] = {}
    for component, base_name in enumerate(BASES):
        terms: list[str] = []
        if particular[component] != 0:
            terms.append(format_fraction(particular[component]))
        for label, coeff_vec in basis:
            c = coeff_vec[component]
            if c == 0:
                continue
            if c == 1:
                terms.append(label)
            elif c == -1:
                terms.append(f"-{label}")
            else:
                terms.append(f"({format_fraction(c)})*{label}")
        components[base_name] = " + ".join(terms).replace("+ -", "- ") or "0"
    return {
        "particular": [format_fraction(c) for c in particular],
        "parameters": [label for label, _ in basis],
        "per_base_component": components,
        "text": ", ".join(f"{b}: {expr}" for b, expr in components.items()),
    }


# --------------------------------------------------------------------- #
# 矛盾诊断
# --------------------------------------------------------------------- #


def _build_conflicts(
    store: SourceStore, system: CompiledSystem, result: SolveResult
) -> list[dict[str, Any]]:
    if result.consistent:
        return []
    item_index = {item.item_id: item for item in system.items}
    out: list[dict[str, Any]] = []

    minimal = result.minimal_items or []
    for conflict in result.conflicts:
        out.append(_conflict_dict(store, system, conflict, minimal))

    minimal_set = set(minimal)
    out.append(
        {
            "kind": "minimal_conflict_set",
            "items": [_item_summary(store, item_index[i]) for i in minimal],
            "item_ids": minimal,
            "minimality_checks": result.minimality_removals,
        }
    )
    return out


def _conflict_dict(
    store: SourceStore,
    system: CompiledSystem,
    conflict: ComponentConflict,
    minimal_items: list[str],
) -> dict[str, Any]:
    combo: list[dict[str, Any]] = []
    for term in conflict.combination:
        con = system.constraints[term.constraint_id]
        combo.append(
            {
                "constraint_index": term.constraint_id,
                "coefficient": format_fraction(term.coefficient),
                "item_id": con.item_id,
                "kind": con.kind,
                "description": con.description,
                "span": _span_dict(con.span),
                "snippet": store.snippet(con.span),
            }
        )
    # 独立重放：用原始标量行再算一次，必须复现非零残差。
    replay = _replay(system, conflict)
    return {
        "kind": "linear_combination_conflict",
        "base_dimension": conflict.base,
        "equation": (
            " + ".join(
                _term_text(t.coefficient, t.constraint_id, system)
                for t in conflict.combination
            )
            + f"  =>  0 = {format_fraction(conflict.residual)}"
        ),
        "residual": format_fraction(conflict.residual),
        "replay_residual": format_fraction(replay),
        "replay_matches": replay == conflict.residual,
        "combination": combo,
        "items_involved": sorted({c["item_id"] for c in combo}),
        "inside_minimal_set": sorted(
            {c["item_id"] for c in combo if c["item_id"] in set(minimal_items)}
        ),
    }


def _term_text(coefficient: Fraction, cid: int, system: CompiledSystem) -> str:
    con = system.constraints[cid]
    return f"({format_fraction(coefficient)})·行{cid}[{con.kind}]"


def _replay(system: CompiledSystem, conflict: ComponentConflict) -> Fraction:
    """从原始约束独立重建标量行并按证书系数线性组合，核对残差。"""
    index = {name: j for j, name in enumerate(system.variables)}
    component = BASES.index(conflict.base)
    residual = Fraction(0)
    lhs_all_zero = True
    for term in conflict.combination:
        con = system.constraints[term.constraint_id]
        row = {index[v]: Fraction(c) for v, c in con.terms}
        if row:
            lhs_all_zero = False
        residual += term.coefficient * Fraction(con.rhs[component])
        # 线性组合后的 LHS 必须为零向量，这里逐变量核验。
    combined: dict[int, Fraction] = {}
    for term in conflict.combination:
        con = system.constraints[term.constraint_id]
        for var_name, value in con.terms:
            j = index[var_name]
            combined[j] = combined.get(j, Fraction(0)) + term.coefficient * Fraction(value)
    assert all(v == 0 for v in combined.values()), "证书线性组合的 LHS 不为零"
    assert residual != 0, "证书残差为零，不是矛盾"
    return residual


def _item_summary(store: SourceStore, item: Item) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "kind": item.kind,
        "name": item.name,
        "file": item.file,
        "span": _span_dict(item.span),
        "snippet": store.snippet(item.span),
    }


def _build_rejected(store: SourceStore, system: CompiledSystem) -> list[dict[str, Any]]:
    return [
        {
            "item_id": item.item_id,
            "kind": item.kind,
            "name": item.name,
            "reason": item.reason,
            "span": _span_dict(item.span),
            "snippet": store.snippet(item.span),
        }
        for item in system.items
        if not item.accepted
    ]


# --------------------------------------------------------------------- #
# JSON 化
# --------------------------------------------------------------------- #


def to_dict(result: CheckResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "base_dimensions": list(BASES),
        "diagnostics": [d.to_dict() for d in result.diagnostics.items],
        "variables": result.variables,
        "conflicts": result.conflicts,
        "rejected_items": result.rejected_items,
        "files": sorted(result.sources._texts.keys()),
    }


# --------------------------------------------------------------------- #
# explain
# --------------------------------------------------------------------- #


def explain(result: CheckResult) -> str:
    store = result.sources
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("量纲检查说明（M / L / T / Theta，四维有理向量）")
    lines.append("=" * 72)
    lines.append(f"总体状态：{_status_text(result.status)}")
    lines.append(f"读取文件：{', '.join(sorted(store._texts.keys()))}")
    lines.append("")

    if result.diagnostics.items:
        lines.append("一、解析与语义诊断（与量纲矛盾分开列出）")
        lines.append("-" * 72)
        for diag in result.diagnostics.items:
            lines.append(
                f"[{diag.severity.value}] {diag.code} {diag.span}: {diag.message}"
            )
            if diag.snippet:
                lines.append(diag.snippet)
            lines.append("")
    else:
        lines.append("一、解析与语义诊断：无")
        lines.append("")

    lines.append("二、变量量纲结论")
    lines.append("-" * 72)
    if not result.solve.consistent:
        lines.append("系统不相容，不输出任何貌似确定的单位；变量清单仅列来源：")
        for var in result.variables:
            if var["status"] == "meta":
                lines.append(
                    f"  标量自由度无法定义（矛盾系统）；"
                    f"自由维数变量: {', '.join(var['free_dimension_variables']) or '无'}"
                )
                continue
            src = ", ".join(f"{s['file']}:{s['line']}:{s['column']}" for s in var["sources"])
            lines.append(f"  {var['name']}: 未决（{src}）")
    else:
        for var in result.variables:
            if var["status"] == "meta":
                lines.append(
                    f"  自由度：命名自由维数变量 {var['free_dimension_variables'] or '[]'}"
                    f"，零空间秩 {var['nullspace_rank']}"
                    f"（标量自由度 {var['scalar_degrees_of_freedom']} = "
                    f"4 × {var['nullspace_rank']}）"
                )
                if var["internal_temp_free_variables"]:
                    lines.append(
                        f"    其中经临时节点产生的自由参数：{var['internal_temp_free_variables']}"
                    )
                continue
            lines.append(f"  {var['name']} [{_var_status_text(var['status'])}]")
            if var["parametric"] is None:
                lines.append(f"      量纲 = {var['dimension_text']}  ({var['dimension']})")
            else:
                lines.append("      量纲未被唯一确定，参数化结果：")
                lines.append(f"        {var['parametric']['text']}")
                lines.append(
                    f"      自由参数：{', '.join(var['parametric']['parameters'])}；"
                    "欠定变量不默认无量纲"
                )
            deps = var["depends_on_items"]
            if deps:
                lines.append("      结论依赖的来源 statement：")
                for item_id in deps:
                    lines.append(f"        - {item_id}")
            src = ", ".join(f"{s['file']}:{s['line']}:{s['column']}" for s in var["sources"])
            lines.append(f"      出现位置：{src}")
    lines.append("")

    lines.append("三、矛盾诊断")
    lines.append("-" * 72)
    if not result.conflicts:
        lines.append("无量纲矛盾。")
    else:
        for entry in result.conflicts:
            if entry["kind"] == "linear_combination_conflict":
                lines.append(
                    f"基础维数 {entry['base_dimension']} 上的线性组合冲突："
                )
                lines.append(f"  {entry['equation']}")
                lines.append(
                    f"  独立重放残差 = {entry['replay_residual']}（"
                    f"{'复现成功' if entry['replay_matches'] else '复现失败'}）"
                )
                for term in entry["combination"]:
                    lines.append(
                        f"    系数 {term['coefficient']:>4} × {term['item_id']}"
                        f"（{term['description']}）"
                    )
                    lines.append("      " + term["snippet"].replace("\n", "\n      "))
            else:
                lines.append("包含极小矛盾 statement 集合（删其中任一条即相容）：")
                for item in entry["items"]:
                    lines.append(f"  - {item['item_id']} ({item['kind']} {item['name']})")
                    lines.append("    " + item["snippet"].replace("\n", "\n    "))
                lines.append("极小性逐条复核：")
                for check_ in entry["minimality_checks"]:
                    lines.append(
                        f"  删除 {check_['removed_item']} -> "
                        f"{'相容' if check_['consistent_after_removal'] else '仍矛盾'}，"
                        f"剩余矛盾行 {check_['remaining_conflict_count']}"
                    )
    lines.append("")
    lines.append("说明：本工具不做数值方程求解、单位换算或温标转换；")
    lines.append("公式原文未被自动修改，解析失败的语句不参与量纲求解。")
    return "\n".join(lines)


def _status_text(status: str) -> str:
    return {
        "consistent": "相容，全部结论可复算",
        "inconsistent": "存在量纲矛盾",
        "parse_error_only": "存在被拒绝的语句（其余部分量纲相容）",
    }[status]


def _var_status_text(status: str) -> str:
    return {
        "declared": "声明已知",
        "uniquely_inferred": "唯一推断",
        "free": "带自由维数",
        "unresolved_conflict": "矛盾未决",
    }[status]
