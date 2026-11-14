"""精确最小化键级差的双射求解器。

目标（来自 README/plan.json，纯数据整理约定，不代表反应机理）：

    min  Σ_{无序反应物原子对 {u,u'}} |b_R(u,u') - b_P(phi(u),phi(u'))|

其中 b 为键级，未成键记 0；双射 phi 必须保持 (原子序数, 同位素)。

实现方式
--------
按 (原子序数, 同位素) 把原子分成互不相交的匹配组，逐组做分支定界 DFS：
已确定的前序组作为前缀传入，使跨组原子对（不同元素间的键）与组内
原子对一样进入增量计费，从而每条原子对恰好计费一次，得到的是完整
双射的全局最优，而不是逐原子贪心或逐组局部最优。

* 剪枝：部分映射代价严格大于已知最优完整解时才剪掉（等代价前缀仍可能
  长成更优的完整解）；
* 不读取坐标，不使用化学评分模型，也不调用任何现成 reaction atom mapper；
* 同值裁决使用 RDKit canonical rank（对输入原子编号置换稳定），
  主映射不依赖文件里的原子编号顺序；最多保留一份同值替代。
"""

from __future__ import annotations

from dataclasses import dataclass

from rdkit import Chem

from .models import Atom, Reaction


@dataclass(frozen=True)
class Mapping:
    """一个完整双射：reactant 全局序号 -> product 全局序号。"""

    pairs: tuple[tuple[int, int], ...]  # 已按 reactant 全局序号排序
    cost: int

    def product_for(self, reactant_global: int) -> int:
        return dict(self.pairs)[reactant_global]


def _side_rank(reaction: Reaction, side: str) -> list[int]:
    """返回该侧每个全局原子的 canonical rank（对原子编号置换稳定）。

    把该侧全部组件合并为一个 RDKit molecule 后统一排名；
    不同组件互为独立片段，rank 仍全局唯一（breakTies=True）。
    """
    merged: Chem.RWMol | None = None
    for mol in reaction.molecules(side):
        rd_mol = Chem.MolFromMolBlock(mol.mol_block, removeHs=False,
                                      strictParsing=False)
        if rd_mol is None:
            raise ValueError(f"{side}:{mol.molecule_index} 无法重新解析")
        merged = Chem.RWMol(rd_mol) if merged is None else \
            Chem.RWMol(Chem.CombineMols(merged, rd_mol))
    assert merged is not None
    ranks = Chem.CanonicalRankAtoms(
        merged, breakTies=True, includeChirality=False
    )
    return [int(r) for r in ranks]


def _incremental_cost(reactant_idx: int, product_idx: int,
                      assignment: dict[int, int],
                      r_bonds: dict[frozenset[int], int],
                      p_bonds: dict[frozenset[int], int]) -> int:
    """把 reactant_idx -> product_idx 追加到当前部分双射时新增的目标值。

    只统计 reactant_idx 与此前已分配原子之间的原子对，
    因此每条原子对在整个 DFS 中恰好计费一次。
    """
    added = 0
    for other_r, other_p in assignment.items():
        r_order = r_bonds.get(frozenset((reactant_idx, other_r)), 0)
        p_order = p_bonds.get(frozenset((product_idx, other_p)), 0)
        added += abs(r_order - p_order)
    return added


def _signature(assignment: dict[int, int],
               r_rank: list[int], p_rank: list[int]) -> tuple[tuple[int, int], ...]:
    """与文件原子编号无关的映射签名：按两侧 canonical rank 表达。"""
    return tuple(sorted((r_rank[r], p_rank[p]) for r, p in assignment.items()))


def solve(reaction: Reaction) -> tuple[Mapping, Mapping | None, int]:
    """求全反应最优主映射。

    返回 ``(主映射, 同值替代或 None, 找到的同值最优解数量，上限 2)``。
    """
    r_atoms: list[Atom] = reaction.atoms("reactant")
    p_atoms: list[Atom] = reaction.atoms("product")
    r_bonds = reaction.bond_order_map("reactant")
    p_bonds = reaction.bond_order_map("product")
    r_rank = _side_rank(reaction, "reactant")
    p_rank = _side_rank(reaction, "product")

    def grouped(atoms: list[Atom]) -> dict[tuple[int, int], list[int]]:
        result: dict[tuple[int, int], list[int]] = {}
        for global_idx, atom in enumerate(atoms):
            result.setdefault(atom.invariant, []).append(global_idx)
        return result

    r_groups = grouped(r_atoms)
    p_groups = grouped(p_atoms)
    if set(r_groups) != set(p_groups) or \
            {k: len(v) for k, v in r_groups.items()} != \
            {k: len(v) for k, v in p_groups.items()}:
        raise ValueError("不变量分组不一致（预检应已拦截）")

    # 大组先解，剪枝更早生效；组内按 canonical rank 排序，
    # 让枚举顺序只依赖图，不依赖文件原子编号。
    ordered_keys = sorted(r_groups, key=lambda k: (-len(r_groups[k]), k))
    r_order: list[int] = []
    candidates_of: dict[int, list[int]] = {}
    for key in ordered_keys:
        group_r = sorted(r_groups[key], key=lambda i: r_rank[i])
        group_p = sorted(p_groups[key], key=lambda i: p_rank[i])
        r_order.extend(group_r)
        for r in group_r:
            candidates_of[r] = list(group_p)

    limit_alternates = 1  # 至多保留主映射 + 一份同值替代
    best_cost: int | None = None
    best: list[dict[int, int]] = []
    assignment: dict[int, int] = {}
    used_product: set[int] = set()

    def dfs(position: int, current_cost: int) -> None:
        nonlocal best_cost, best
        # 严格大于才剪：等代价的部分映射补全后仍可能更优。
        if best_cost is not None and current_cost > best_cost:
            return
        if position == len(r_order):
            if best_cost is None or current_cost < best_cost:
                best_cost = current_cost
                best = [dict(assignment)]
            elif current_cost == best_cost and len(best) <= limit_alternates:
                best.append(dict(assignment))
            return
        reactant_idx = r_order[position]
        for product_idx in candidates_of[reactant_idx]:
            if product_idx in used_product:
                continue  # 双射约束：每个产物原子至多被选一次
            added = _incremental_cost(
                reactant_idx, product_idx, assignment, r_bonds, p_bonds
            )
            assignment[reactant_idx] = product_idx
            used_product.add(product_idx)
            dfs(position + 1, current_cost + added)
            used_product.remove(product_idx)
            del assignment[reactant_idx]

    dfs(0, 0)
    if best_cost is None:
        raise RuntimeError("求解失败（不应发生，预检应已拦截）")

    # 同值裁决：canonical-rank 签名字典序最小者为主映射，
    # 该顺序只取决于图与元素/同位素，不取决于输入文件原子编号。
    ranked = sorted(best, key=lambda a: _signature(a, r_rank, p_rank))
    main = Mapping(pairs=tuple(sorted(ranked[0].items())), cost=best_cost)
    alternate = None
    if len(ranked) > 1:
        alternate = Mapping(pairs=tuple(sorted(ranked[1].items())),
                            cost=best_cost)
    return main, alternate, len(ranked)


def objective_value(reaction: Reaction, mapping: Mapping) -> int:
    """独立按定义重算目标值，供核验入口与求解结果交叉对账。"""
    r_bonds = reaction.bond_order_map("reactant")
    p_bonds = reaction.bond_order_map("product")
    phi = dict(mapping.pairs)
    r_atoms = reaction.atoms("reactant")
    total = 0
    for i in range(len(r_atoms)):
        for j in range(i + 1, len(r_atoms)):
            r_order = r_bonds.get(frozenset((i, j)), 0)
            p_order = p_bonds.get(frozenset((phi[i], phi[j])), 0)
            total += abs(r_order - p_order)
    return total
