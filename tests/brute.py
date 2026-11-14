"""测试辅助：独立暴力枚举（itertools 全排列），与生产求解器完全分开。

只用于小反应的最优值交叉核对，不参与生产路径。
"""

from __future__ import annotations

from itertools import product

from map_reaction.models import Reaction


def brute_force_optimum(reaction: Reaction) -> tuple[int, int]:
    """枚举全部合法 (元素, 同位素) 保持双射，返回 (最优值, 最优解数量)。"""
    r_atoms = reaction.atoms("reactant")
    r_bonds = reaction.bond_order_map("reactant")
    p_bonds = reaction.bond_order_map("product")

    r_groups: dict[tuple[int, int], list[int]] = {}
    p_groups: dict[tuple[int, int], list[int]] = {}
    for i, atom in enumerate(r_atoms):
        r_groups.setdefault(atom.invariant, []).append(i)
    for i, atom in enumerate(reaction.atoms("product")):
        p_groups.setdefault(atom.invariant, []).append(i)

    keys = list(r_groups)
    group_permutations = [
        [dict(zip(r_groups[k], perm))
         for perm in _permutations(p_groups[k])]
        for k in keys
    ]

    best_cost: int | None = None
    n_best = 0
    n_atoms = len(r_atoms)
    for choices in product(*group_permutations):
        assignment: dict[int, int] = {}
        for piece in choices:
            assignment.update(piece)
        cost = 0
        for i in range(n_atoms):
            for j in range(i + 1, n_atoms):
                cost += abs(
                    r_bonds.get(frozenset((i, j)), 0)
                    - p_bonds.get(frozenset((assignment[i], assignment[j])), 0)
                )
        if best_cost is None or cost < best_cost:
            best_cost, n_best = cost, 1
        elif cost == best_cost:
            n_best += 1
    assert best_cost is not None
    return best_cost, n_best


def _permutations(items: list[int]):
    """标准库 permutations 的薄封装，便于测试阅读。"""
    from itertools import permutations
    yield from permutations(items)
