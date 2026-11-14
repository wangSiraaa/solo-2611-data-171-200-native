"""精确有理线性求解（逐分量 Gauss-Jordan），不依赖符号数学平台。

每条原始约束展开成 M/L/T/Theta 四个标量行，四个分量各自独立消元；
行携带来源 provenance（约束 id 与组合系数），矛盾行因此能回溯到具体
声明/命名公式的 span。求解结果给出：

* 相容时的一个特解 + 齐次零空间基（参数化自由维数）；
* 矛盾时的原始线性组合证书，以及按 statement 计的包含极小矛盾集合，
  并逐条复核“删其中任一 statement 即相容”。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

from .compiler import CompiledSystem

BASES = ("M", "L", "T", "Theta")
_ZERO = Fraction(0)
_ONE = Fraction(1)
_TEMP_RE = re.compile(r"^t\d+$")


def _is_temp_name(name: str) -> bool:
    return bool(_TEMP_RE.match(name))


@dataclass(frozen=True)
class Term:
    constraint_id: int
    coefficient: Fraction


@dataclass(frozen=True)
class ComponentConflict:
    """一条可复算的线性组合：sum coeff_i * row_i 产生 0 = residual != 0。"""

    base: str
    combination: tuple[Term, ...]
    residual: Fraction


@dataclass
class SolveResult:
    consistent: bool
    variables: list[str]
    particular: dict[str, tuple[Fraction, Fraction, Fraction, Fraction]] | None
    nullspace: list[dict[str, tuple[Fraction, Fraction, Fraction, Fraction]]]
    pivot_variables: list[str]
    free_variables: list[str]
    conflicts: list[ComponentConflict]
    minimal_items: list[str] | None
    minimality_removals: list[dict]


@dataclass
class _Row:
    coeff: list[Fraction]
    rhs: Fraction
    prov: tuple[Term, ...]
    base: str
    source_constraints: tuple[int, ...]  # 本行由哪些原始约束构成（消元前为单元素）


def _scalar_rows(system: CompiledSystem) -> list[list[_Row]]:
    index = {name: j for j, name in enumerate(system.variables)}
    nvar = len(system.variables)
    blocks: list[list[_Row]] = [[] for _ in BASES]
    for cid, con in enumerate(system.constraints):
        for component, base in enumerate(BASES):
            coeff = [_ZERO] * nvar
            for var_name, value in con.terms:
                coeff[index[var_name]] += Fraction(value)
            rhs = Fraction(con.rhs[component])
            if not any(c != 0 for c in coeff) and rhs == 0:
                continue
            blocks[component].append(
                _Row(coeff, rhs, (Term(cid, _ONE),), base, (cid,))
            )
    return blocks


def _combine_prov(prov_a, factor_a, prov_b, factor_b) -> tuple[Term, ...]:
    merged: dict[int, Fraction] = {}
    for term in prov_a:
        merged[term.constraint_id] = (
            merged.get(term.constraint_id, _ZERO) + factor_a * term.coefficient
        )
    for term in prov_b:
        merged[term.constraint_id] = (
            merged.get(term.constraint_id, _ZERO) + factor_b * term.coefficient
        )
    return tuple(
        Term(cid, coef) for cid, coef in sorted(merged.items()) if coef != 0
    )


@dataclass
class _Elimination:
    pivot_cols: list[int]
    pivot_rhs: list[Fraction]
    # 主元行（与 pivot_cols 同序）：行形如 x_pivot + sum a_j x_j = rhs
    pivot_rows: list[list[Fraction]]
    conflicts: list[ComponentConflict]


def _eliminate_block(rows: list[_Row], nvar: int, base: str) -> _Elimination:
    """两阶段 Gauss-Jordan。

    阶段一：选主元、归一化、只消去主元位置**下方**的行（前向消元）。
    阶段二：自底向上回代，消去每个主元列在**上方**的系数。
    主元行一旦在阶段一定型，阶段二只改更靠上的行，因此旧主元行不会被污染；
    provenance 随每一步行变换同步组合，零行自然携带完整线性组合证书。
    """
    work = [
        _Row(list(r.coeff), r.rhs, r.prov, r.base, r.source_constraints)
        for r in rows
    ]
    pivot_cols: list[int] = []
    pivot_row_indexes: list[int] = []
    conflicts: list[ComponentConflict] = []
    pivot_row = 0

    # ---- 阶段一：前向消元 ---------------------------------------------
    for col in range(nvar):
        candidate = next((r for r in work[pivot_row:] if r.coeff[col] != 0), None)
        if candidate is None:
            continue
        swap_pos = work.index(candidate, pivot_row)
        work[pivot_row], work[swap_pos] = work[swap_pos], work[pivot_row]
        pivot = work[pivot_row]
        scale = pivot.coeff[col]
        pivot.coeff = [c / scale for c in pivot.coeff]
        pivot.rhs /= scale
        pivot.prov = tuple(
            Term(t.constraint_id, t.coefficient / scale) for t in pivot.prov
        )
        for r_idx in range(pivot_row + 1, len(work)):
            row = work[r_idx]
            factor = row.coeff[col]
            if factor == 0:
                continue
            row.coeff = [c - factor * p for c, p in zip(row.coeff, pivot.coeff)]
            row.rhs -= factor * pivot.rhs
            row.prov = _combine_prov(row.prov, _ONE, pivot.prov, -factor)
        pivot_cols.append(col)
        pivot_row_indexes.append(pivot_row)
        pivot_row += 1

    # 系数全零的行：rhs != 0 即矛盾，provenance 就是冲突线性组合。
    for row in work[pivot_row:]:
        if not any(c != 0 for c in row.coeff) and row.rhs != 0:
            conflicts.append(ComponentConflict(base, tuple(row.prov), row.rhs))

    # ---- 阶段二：回代，消去主元列上方的系数 ---------------------------
    for k in range(len(pivot_cols) - 1, -1, -1):
        pivot = work[pivot_row_indexes[k]]
        col = pivot_cols[k]
        for r_idx in range(pivot_row_indexes[k]):
            row = work[r_idx]
            factor = row.coeff[col]
            if factor == 0:
                continue
            row.coeff = [c - factor * p for c, p in zip(row.coeff, pivot.coeff)]
            row.rhs -= factor * pivot.rhs
            row.prov = _combine_prov(row.prov, _ONE, pivot.prov, -factor)

    pivot_rhs = [work[i].rhs for i in pivot_row_indexes]
    pivot_rows = [list(work[i].coeff) for i in pivot_row_indexes]
    return _Elimination(pivot_cols, pivot_rhs, pivot_rows, conflicts)


def solve(system: CompiledSystem) -> SolveResult:
    nvar = len(system.variables)
    blocks = _scalar_rows(system)
    eliminations = [_eliminate_block(block, nvar, base) for block, base in zip(blocks, BASES)]
    all_conflicts = [c for elim in eliminations for c in elim.conflicts]
    consistent = not all_conflicts

    pivot_set = {col for elim in eliminations for col in elim.pivot_cols}
    pivot_variables = [name for j, name in enumerate(system.variables) if j in pivot_set]
    free_variables = [name for j, name in enumerate(system.variables) if j not in pivot_set]

    particular = None
    nullspace: list[dict[str, tuple[Fraction, ...]]] = []
    minimal_items = None
    removals: list[dict] = []

    if consistent:
        particular, nullspace = _build_solution(
            system.variables, free_variables, eliminations
        )
        # 剔除在所有实（命名）变量上恒为零的零空间基：那只是孤立临时节点
        # （如未绑定命名变量的函数返回值 t_n）的内部自由度，不构成对外自由维数，
        # 也不能把欠定的命名变量默认成无量纲。
        kept: list[tuple[str, dict]] = []
        for free_name, basis in zip(free_variables, nullspace):
            if any(
                any(c != 0 for c in vec)
                for name, vec in basis.items()
                if not _is_temp_name(name)
            ):
                kept.append((free_name, basis))
        free_variables = [name for name, _ in kept]
        nullspace = [basis for _, basis in kept]
    else:
        minimal_items, removals = _minimal_conflict_items(system, all_conflicts)

    return SolveResult(
        consistent=consistent,
        variables=list(system.variables),
        particular=particular,
        nullspace=nullspace,
        pivot_variables=pivot_variables,
        free_variables=free_variables,
        conflicts=all_conflicts,
        minimal_items=minimal_items,
        minimality_removals=removals,
    )


def _build_solution(variables, free_variables, eliminations):
    nvar = len(variables)

    # 特解：自由变量取 0，主元变量按分量取各自 RHS，逐分量填充不覆盖。
    particular: dict[str, list[Fraction]] = {
        name: [_ZERO] * 4 for name in variables
    }
    for component, elim in enumerate(eliminations):
        for col, rhs in zip(elim.pivot_cols, elim.pivot_rhs):
            particular[variables[col]][component] = rhs

    # 零空间：每个自由变量（在每个分量上）生成一个基向量。
    nullspace_raw: list[dict[str, list[Fraction]]] = []
    for free_name in free_variables:
        free_col = variables.index(free_name)
        basis = {name: [_ZERO] * 4 for name in variables}
        basis[free_name] = [_ONE] * 4
        for component, elim in enumerate(eliminations):
            if free_col in elim.pivot_cols:
                continue
            for pivot_col, coeff_row in zip(elim.pivot_cols, elim.pivot_rows):
                # 主元行：x_pivot = rhs - sum_j coeff[j] x_j；齐次取负号。
                basis[variables[pivot_col]][component] = -coeff_row[free_col]
        nullspace_raw.append(basis)

    particular_out = {name: tuple(vec) for name, vec in particular.items()}
    nullspace_out = [
        {name: tuple(vec) for name, vec in basis.items()} for basis in nullspace_raw
    ]
    return particular_out, nullspace_out


def _minimal_conflict_items(
    system: CompiledSystem, conflicts: list[ComponentConflict]
) -> tuple[list[str], list[dict]]:
    item_of_constraint = {
        cid: con.item_id for cid, con in enumerate(system.constraints)
    }
    all_items = {item.item_id for item in system.items if item.accepted}
    candidate: list[str] = []
    seen: set[str] = set()
    for conflict in conflicts:
        for term in conflict.combination:
            item_id = item_of_constraint[term.constraint_id]
            if item_id not in seen:
                seen.add(item_id)
                candidate.append(item_id)

    def whole_system_consistent_without(removed: set[str]) -> tuple[bool, int]:
        kept = all_items - removed
        return _consistent_with_items(system, kept)

    # 包含极小化（相对整个系统）：若全系统删掉 item 后仍矛盾，
    # 则 item 对“恢复相容性”不是必要的，可从责任集合中剥离。
    current = list(candidate)
    changed = True
    while changed:
        changed = False
        for item_id in list(current):
            ok, _ = whole_system_consistent_without({item_id})
            if not ok:
                current.remove(item_id)
                changed = True

    # 极小性复核：对最终集合里的每条 statement，全系统删它之后必须相容。
    removals: list[dict] = []
    for item_id in current:
        ok, conflict_count = whole_system_consistent_without({item_id})
        removals.append(
            {
                "removed_item": item_id,
                "consistent_after_removal": ok,
                "remaining_conflict_count": conflict_count,
            }
        )
    return current, removals


def _consistent_with_items(
    system: CompiledSystem, kept_items: set[str]
) -> tuple[bool, int]:
    """只保留 kept_items 中的 statement 约束重新消元。"""
    nvar = len(system.variables)
    index = {name: j for j, name in enumerate(system.variables)}
    conflict_count = 0
    for component in range(4):
        rows: list[_Row] = []
        for cid, con in enumerate(system.constraints):
            if con.item_id not in kept_items:
                continue
            coeff = [_ZERO] * nvar
            for var_name, value in con.terms:
                coeff[index[var_name]] += Fraction(value)
            rhs = Fraction(con.rhs[component])
            if not any(c != 0 for c in coeff) and rhs == 0:
                continue
            rows.append(_Row(coeff, rhs, (Term(cid, _ONE),), BASES[component], (cid,)))
        elim = _eliminate_block(rows, nvar, BASES[component])
        conflict_count += len(elim.conflicts)
    return conflict_count == 0, conflict_count
