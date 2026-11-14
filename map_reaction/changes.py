"""从映射重算键变化与原子属性 before/after 表。"""

from __future__ import annotations

from .models import Reaction
from .solver import Mapping


def _ref(reaction: Reaction, side: str, global_index: int):
    return reaction.atoms(side)[global_index].ref


def bond_changes(reaction: Reaction, mapping: Mapping) -> list[dict]:
    """逐映射原子对报告断键、成键、键级变化。

    枚举反应物侧重原子的全部无序原子对，按映射取产物侧对应原子对，
    比较两侧键级（未成键为 0），只保留发生变化的对。
    """
    r_bonds = reaction.bond_order_map("reactant")
    p_bonds = reaction.bond_order_map("product")
    phi = dict(mapping.pairs)
    n = len(reaction.atoms("reactant"))
    rows: list[dict] = []
    for i in range(n):
        for j in range(i + 1, n):
            r_order = r_bonds.get(frozenset((i, j)), 0)
            pi, pj = phi[i], phi[j]
            p_order = p_bonds.get(frozenset((pi, pj)), 0)
            if r_order == p_order:
                continue
            if r_order == 0:
                change_type = "bond_formed"
            elif p_order == 0:
                change_type = "bond_broken"
            else:
                change_type = "order_changed"
            rows.append({
                "type": change_type,
                "reactant_atoms": [_ref(reaction, "reactant", i).as_string(),
                                   _ref(reaction, "reactant", j).as_string()],
                "product_atoms": [_ref(reaction, "product", pi).as_string(),
                                  _ref(reaction, "product", pj).as_string()],
                "map_numbers": sorted((i + 1, j + 1)),
                "bond_order_before": r_order,
                "bond_order_after": p_order,
                "abs_order_delta": abs(r_order - p_order),
            })
    rows.sort(key=lambda row: (row["map_numbers"], row["type"]))
    return rows


def atom_table(reaction: Reaction, mapping: Mapping) -> list[dict]:
    """每个映射编号一行：charge/isotope/显式与隐式 H 的 before/after。"""
    r_atoms = reaction.atoms("reactant")
    p_atoms = reaction.atoms("product")
    rows: list[dict] = []
    for r_global, p_global in mapping.pairs:
        before = r_atoms[r_global]
        after = p_atoms[p_global]
        rows.append({
            "map_number": r_global + 1,
            "element": before.symbol,
            "reactant_ref": before.ref.as_string(),
            "product_ref": after.ref.as_string(),
            "isotope_before": before.isotope,
            "isotope_after": after.isotope,
            "charge_before": before.formal_charge,
            "charge_after": after.formal_charge,
            "explicit_h_before": before.explicit_hs,
            "explicit_h_after": after.explicit_hs,
            "implicit_h_before": before.implicit_hs,
            "implicit_h_after": after.implicit_hs,
            "charge_changed": before.formal_charge != after.formal_charge,
            "implicit_h_changed": before.implicit_hs != after.implicit_hs,
        })
    return rows


def summarize(reaction: Reaction, mapping: Mapping,
              alternate: Mapping | None, n_optimal: int,
              precheck_report: dict) -> dict:
    """汇总一份可序列化的映射报告。"""
    return {
        "source_rxn": reaction.source_path,
        "objective": "sum abs(reactant bond order - mapped product bond order); "
                     "missing bond = 0",
        "mechanism_claim": "本结果只是键级差数据整理约定下的最优映射，"
                           "不构成反应机理发现",
        "precheck": precheck_report,
        "cost": mapping.cost,
        "n_optimal_mappings_found": n_optimal,
        "ambiguous": alternate is not None,
        "ambiguity_note": (
            "存在至少一份同键级差目标值的替代双射；主映射按与文件原子编号"
            "无关的 canonical-rank 字典序稳定选取，文件原子编号不作为唯一性"
            "证据，对称原子互换不代表可区分的化学结论"
        ) if alternate is not None else "未发现同值替代（在保留的前两份最优解内）",
        "main_mapping": [
            {"map_number": r + 1,
             "reactant": _ref(reaction, "reactant", r).as_string(),
             "product": _ref(reaction, "product", p).as_string()}
            for r, p in mapping.pairs
        ],
        "alternate_mapping": None if alternate is None else [
            {"map_number": r + 1,
             "reactant": _ref(reaction, "reactant", r).as_string(),
             "product": _ref(reaction, "product", p).as_string()}
            for r, p in alternate.pairs
        ],
        "bond_changes": bond_changes(reaction, mapping),
        "atoms": atom_table(reaction, mapping),
    }
