"""极小反应：手工构造 RXN，独立枚举核对最优值与变化类型。

这些案例不依赖 fixtures 目录，直接在内存里拼 V2000 mol block，
覆盖：无变化、纯断键、纯成键、键级升降、同位素约束、电荷/H 报告。
"""

from __future__ import annotations

import pytest

from map_reaction.changes import bond_changes
from map_reaction.precheck import precheck
from map_reaction.reader import parse_rxn
from map_reaction.solver import objective_value, solve
from tests.brute import brute_force_optimum


def _mol_block(name: str, atoms: list[tuple[str, int]],
               bonds: list[tuple[int, int, int]],
               charges: dict[int, int] | None = None) -> str:
    charges = charges or {}
    lines = [
        name,
        "     RDKit          2D",
        "",
        f"{len(atoms):3d}{len(bonds):3d}  0  0  0  0  0  0  0  0999 V2000",
    ]
    for symbol, isotope in atoms:
        iso = f"{isotope:3d}" if isotope else "  0"
        lines.append(
            f"    0.0000    0.0000    0.0000 {symbol:<3}{iso}  0  0  0  0  0  0  0  0  0  0  0"
        )
    for a, b, order in bonds:
        lines.append(f"{a + 1:3d}{b + 1:3d}{order:3d}  0")
    if charges:
        lines.append(f"M  CHG{len(charges):3d}" +
                     "".join(f"{idx + 1:4d}{chg:4d}"
                             for idx, chg in charges.items()))
    lines.append("M  END")
    return "\n".join(lines) + "\n"


def _rxn(tmp_path, name: str, reactants: list[str], products: list[str]) -> str:
    text = (
        "$RXN\n\n      RDKit\n\n"
        f"{len(reactants):3d}{len(products):3d}\n"
        + "".join(f"$MOL\n{block}" for block in reactants)
        + "".join(f"$MOL\n{block}" for block in products)
    )
    path = tmp_path / f"{name}.rxn"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_no_change_two_atoms(tmp_path):
    block = _mol_block("same", [("C", 0), ("O", 0)], [(0, 1, 1)])
    path = _rxn(tmp_path, "nochange", [block], [block])
    reaction = parse_rxn(path)
    precheck(reaction)
    main, alt, _ = solve(reaction)
    brute, n_brute = brute_force_optimum(reaction)
    assert main.cost == 0 == brute
    assert n_brute == 1 and alt is None
    assert bond_changes(reaction, main) == []


def test_pure_bond_break(tmp_path):
    left = _mol_block("ethane_like", [("C", 0), ("C", 0)], [(0, 1, 1)])
    right = _mol_block("two_c", [("C", 0), ("C", 0)], [])
    path = _rxn(tmp_path, "break", [left], [right])
    reaction = parse_rxn(path)
    main, _, _ = solve(reaction)
    assert main.cost == brute_force_optimum(reaction)[0] == 1
    changes = bond_changes(reaction, main)
    assert len(changes) == 1 and changes[0]["type"] == "bond_broken"


def test_pure_bond_formation(tmp_path):
    left = _mol_block("two_n", [("N", 0), ("N", 0)], [])
    right = _mol_block("nn", [("N", 0), ("N", 0)], [(0, 1, 2)])
    path = _rxn(tmp_path, "form", [left], [right])
    reaction = parse_rxn(path)
    main, _, _ = solve(reaction)
    assert main.cost == brute_force_optimum(reaction)[0] == 2
    changes = bond_changes(reaction, main)
    assert len(changes) == 1 and changes[0]["type"] == "bond_formed"
    assert changes[0]["bond_order_after"] == 2


def test_order_double_to_triple(tmp_path):
    left = _mol_block("cc", [("C", 0), ("C", 0)], [(0, 1, 2)])
    right = _mol_block("cc3", [("C", 0), ("C", 0)], [(0, 1, 3)])
    path = _rxn(tmp_path, "order", [left], [right])
    reaction = parse_rxn(path)
    main, _, _ = solve(reaction)
    assert main.cost == 1
    changes = bond_changes(reaction, main)
    assert changes[0]["type"] == "order_changed"
    assert (changes[0]["bond_order_before"],
            changes[0]["bond_order_after"]) == (2, 3)


def test_isotope_breaks_symmetry(tmp_path):
    # 两个 C 中一个带 13 同位素；产物同构但同位素位置决定唯一双射
    left = _mol_block("iso_l",
                      [("C", 13), ("C", 0), ("O", 0)],
                      [(0, 1, 1), (1, 2, 1)])
    right = _mol_block("iso_r",
                       [("C", 0), ("C", 13), ("O", 0)],
                       [(0, 1, 1), (1, 2, 2)])
    path = _rxn(tmp_path, "iso", [left], [right])
    reaction = parse_rxn(path)
    precheck(reaction)
    main, alt, n_opt = solve(reaction)
    assert alt is None and n_opt == 1  # 同位素锁定，无同值替代
    # 13C 位置在两侧错位一格，两条键的键级差合计为 3
    assert main.cost == brute_force_optimum(reaction)[0] == 3
    # 13C 必须映到 13C
    phi = dict(main.pairs)
    assert phi[0] == 1


def test_charge_and_h_reported_not_matched(tmp_path):
    # 反应物 OH-（显式价态字段使隐式 H=0），产物中性 OH；元素守恒即可映射
    left = (
        "oh_minus\n     RDKit          2D\n\n"
        "  1  0  0  0  0  0  0  0  0  0999 V2000\n"
        "    0.0000    0.0000    0.0000 O   0  0  0  0  0  1  0  0  0  0  0  0\n"
        "M  CHG  1   1  -1\nM  END\n"
    )
    right = _mol_block("oh", [("O", 0)], [])
    path = _rxn(tmp_path, "charge", [left], [right])
    reaction = parse_rxn(path)
    report = precheck(reaction)  # 不因电荷不同而拒绝
    assert report["charge_totals"] == {"reactant": -1, "product": 0}
    main, _, _ = solve(reaction)
    assert objective_value(reaction, main) == 0
    atom_row = reaction.atoms("reactant")[0]
    assert atom_row.formal_charge == -1
    assert reaction.atoms("product")[0].formal_charge == 0


def test_symmetric_three_carbon_chain(tmp_path):
    # C-C-C 与镜像同构：两端 C 互换应产生同值替代
    left = _mol_block("ccc_l",
                      [("C", 0), ("C", 0), ("C", 0)],
                      [(0, 1, 1), (1, 2, 1)])
    right = _mol_block("ccc_r",
                       [("C", 0), ("C", 0), ("C", 0)],
                       [(0, 1, 2), (1, 2, 1)])
    path = _rxn(tmp_path, "sym3", [left], [right])
    reaction = parse_rxn(path)
    main, alt, n_opt = solve(reaction)
    assert main.cost == brute_force_optimum(reaction)[0]
    assert alt is not None and n_opt == 2
    assert objective_value(reaction, alt) == main.cost
    # 主映射与替代在端位碳上互换
    assert dict(main.pairs)[0] != dict(alt.pairs)[0]


@pytest.mark.parametrize("seed", [0, 1, 4])
def test_enumeration_matches_solver_on_random_tiny(tmp_path, seed):
    """随机小图：暴力枚举值始终等于求解器值（含跨组键场景）。"""
    # 2C + 2O 的价态合法小图（隐式 H 补全剩余价态），覆盖跨组 C-O 键
    layouts_left = [
        ([("C", 0), ("C", 0), ("O", 0), ("O", 0)],
         [(0, 1, 1), (1, 2, 1)]),
        ([("C", 0), ("C", 0), ("O", 0), ("O", 0)],
         [(0, 2, 1)]),
        ([("C", 0), ("C", 0), ("O", 0), ("O", 0)],
         [(0, 1, 2), (0, 2, 1)]),
    ]
    layouts_right = [
        ([("C", 0), ("C", 0), ("O", 0), ("O", 0)],
         [(0, 1, 1), (1, 3, 1)]),
        ([("C", 0), ("C", 0), ("O", 0), ("O", 0)],
         [(1, 2, 1), (2, 3, 1)]),
        ([("C", 0), ("C", 0), ("O", 0), ("O", 0)],
         [(0, 2, 2), (1, 2, 1)]),
    ]
    li, ri = seed % 3, (seed * 2 + 1) % 3
    left = _mol_block("l", *layouts_left[li])
    right = _mol_block("r", *layouts_right[ri])
    path = _rxn(tmp_path, f"tiny{seed}", [left], [right])
    reaction = parse_rxn(path)
    precheck(reaction)
    main, alt, _ = solve(reaction)
    brute, brute_n = brute_force_optimum(reaction)
    assert main.cost == brute
    if alt is not None:
        assert objective_value(reaction, alt) == main.cost
        assert brute_n >= 2
