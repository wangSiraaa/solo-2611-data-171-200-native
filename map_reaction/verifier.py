"""独立核验入口：重新解析实际写出的 mapped RXN/SDF 文件。

不读取求解期的内存对象做判断，只从磁盘文件重新解析（仍使用 RDKit
读取，但映射、变化与目标值全部在此重算），核对：
  1. 组件数、组件顺序、每组件原子/键数量与原 RXN 一致；
  2. 每个 map 编号 1..N 在两侧各出现且仅出现一次；
  3. 同编号两侧元素、同位素一致；
  4. 形式电荷、显式/隐式 H 与输入一致（被保留而非重算改写）；
  5. 键结构（原子对+键级）与输入一致，map 只是编号标注；
  6. 从读回映射重算键级差目标，与报告中的 cost 一致。
"""

from __future__ import annotations

from rdkit import Chem

from .models import Molecule, Reaction
from .reader import _build_molecule, _split_mol_blocks_rxn, _split_mol_blocks_sdf

_BOND_ORDER = {
    Chem.BondType.SINGLE: 1,
    Chem.BondType.DOUBLE: 2,
    Chem.BondType.TRIPLE: 3,
}


def _parse_mapped_blocks(path: str) -> tuple[list[Molecule], list[Molecule]]:
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    n_r, n_p, blocks = _split_mol_blocks_rxn(text)
    reactants = [_build_molecule(b, "reactant", i)
                 for i, b in enumerate(blocks[:n_r])]
    products = [_build_molecule(b, "product", i)
                for i, b in enumerate(blocks[n_r:])]
    return reactants, products


def _rd_mols(molecules: list[Molecule]):
    return [Chem.MolFromMolBlock(mol.mol_block, sanitize=False,
                                 removeHs=False, strictParsing=False)
            for mol in molecules]


def _map_numbers(rd_mols) -> list[int]:
    return [atom.GetAtomMapNum() for rd_mol in rd_mols
            for atom in rd_mol.GetAtoms()]


def _edge_set(rd_mols) -> set[tuple[int, int, int]]:
    edges = set()
    offset = 0
    for rd_mol in rd_mols:
        for bond in rd_mol.GetBonds():
            a = offset + bond.GetBeginAtomIdx()
            b = offset + bond.GetEndAtomIdx()
            edges.add((min(a, b), max(a, b), _BOND_ORDER[bond.GetBondType()]))
        offset += rd_mol.GetNumAtoms()
    return edges


def _implicit_hs(rd_mol) -> list[int]:
    """隐式 H 是 sanitize 后的价态推算量，单独做一次 sanitize 解析取得。"""
    sanitized = Chem.Mol(rd_mol)
    Chem.SanitizeMol(sanitized)
    return [atom.GetNumImplicitHs() for atom in sanitized.GetAtoms()]


def verify_files(reaction: Reaction, mapped_rxn_path: str,
                 expected_cost: int) -> dict:
    """重新解析 mapped RXN 并完成全部核验，失败即抛 AssertionError。"""
    failures: list[str] = []
    reactants, products = _parse_mapped_blocks(mapped_rxn_path)

    # 1. 组件、组件名与原子/键数量
    if len(reactants) != len(reaction.reactants) or \
            len(products) != len(reaction.products):
        failures.append("组件数量与原 RXN 不一致")
    for side, got, expected in (
        ("reactant", reactants, reaction.reactants),
        ("product", products, reaction.products),
    ):
        for idx, (g, e) in enumerate(zip(got, expected)):
            if g.name != e.name:
                failures.append(f"{side}:{idx} 组件名未保留")
            if len(g.atoms) != len(e.atoms) or len(g.edges) != len(e.edges):
                failures.append(
                    f"{side}:{idx} 原子/键数量改变 "
                    f"({len(g.atoms)}/{len(g.edges)} vs "
                    f"{len(e.atoms)}/{len(e.edges)})"
                )

    r_rd, p_rd = _rd_mols(reactants), _rd_mols(products)
    r_maps, p_maps = _map_numbers(r_rd), _map_numbers(p_rd)
    n_total = len(r_maps)
    expected_maps = set(range(1, n_total + 1))

    # 2. map 编号 1..N 两侧各一次
    if sorted(r_maps) != sorted(expected_maps):
        failures.append(f"反应物侧 map 编号不是 1..{n_total} 的排列")
    if sorted(p_maps) != sorted(expected_maps):
        failures.append(f"产物侧 map 编号不是 1..{n_total} 的排列")

    # 3/4. 同编号元素/同位素一致；电荷与 H 相对输入逐原子保留
    r_flat = [a for mol in reactants for a in mol.atoms]
    p_flat = [a for mol in products for a in mol.atoms]
    r_by_map = dict(zip(r_maps, r_flat))
    p_by_map = dict(zip(p_maps, p_flat))
    # 写回文件里没有“隐式 H”字段，它是读回时按价态重新推算的，
    # 因此对 mapped 图单独 sanitize 后重算，再与输入侧推算值比较。
    r_implicit = [h for rd_mol in r_rd for h in _implicit_hs(rd_mol)]
    p_implicit = [h for rd_mol in p_rd for h in _implicit_hs(rd_mol)]
    r_implicit_by_map = dict(zip(r_maps, r_implicit))
    p_implicit_by_map = dict(zip(p_maps, p_implicit))
    src_r = {a.ref.as_string(): a for a in reaction.atoms("reactant")}
    src_p = {a.ref.as_string(): a for a in reaction.atoms("product")}
    for map_number in sorted(expected_maps):
        left, right = r_by_map[map_number], p_by_map[map_number]
        if left.invariant != right.invariant:
            failures.append(
                f"map {map_number} 元素/同位素不一致："
                f"{left.symbol}/{left.isotope} vs "
                f"{right.symbol}/{right.isotope}"
            )
        src_left, src_right = src_r[left.ref.as_string()], \
            src_p[right.ref.as_string()]
        for field_name in ("formal_charge", "explicit_hs", "isotope"):
            if getattr(left, field_name) != getattr(src_left, field_name):
                failures.append(
                    f"map {map_number} 反应物 {field_name} 写回后改变")
            if getattr(right, field_name) != getattr(src_right, field_name):
                failures.append(
                    f"map {map_number} 产物 {field_name} 写回后改变")
        if r_implicit_by_map[map_number] != src_left.implicit_hs:
            failures.append(
                f"map {map_number} 反应物隐式 H 读回推算值 "
                f"{r_implicit_by_map[map_number]} 与输入 "
                f"{src_left.implicit_hs} 不一致")
        if p_implicit_by_map[map_number] != src_right.implicit_hs:
            failures.append(
                f"map {map_number} 产物隐式 H 读回推算值 "
                f"{p_implicit_by_map[map_number]} 与输入 "
                f"{src_right.implicit_hs} 不一致")

    # 5. 键结构与输入逐边一致
    if _edge_set(r_rd) != _edge_set(_rd_mols(reaction.reactants)):
        failures.append("反应物侧键结构与输入不一致")
    if _edge_set(p_rd) != _edge_set(_rd_mols(reaction.products)):
        failures.append("产物侧键结构与输入不一致")

    # 6. 从读回映射独立重算目标值
    phi = {r_flat.index(r_by_map[k]): p_flat.index(p_by_map[k])
           for k in expected_maps}
    r_bonds = reaction.bond_order_map("reactant")
    p_bonds = reaction.bond_order_map("product")
    recomputed = 0
    for i in range(n_total):
        for j in range(i + 1, n_total):
            recomputed += abs(
                r_bonds.get(frozenset((i, j)), 0)
                - p_bonds.get(frozenset((phi[i], phi[j])), 0)
            )
    if recomputed != expected_cost:
        failures.append(
            f"读回重算目标 {recomputed} 与报告 cost {expected_cost} 不一致")

    if failures:
        raise AssertionError("核验失败：" + "；".join(failures))
    return {
        "mapped_rxn": mapped_rxn_path,
        "n_map_numbers": n_total,
        "map_numbers_1_to_n_each_side": True,
        "element_isotope_preserved": True,
        "charge_isotope_and_h_preserved": True,
        "bond_structure_preserved": True,
        "recomputed_objective": recomputed,
        "matches_reported_cost": recomputed == expected_cost,
    }


def _sdf_fields(block: str) -> dict[str, str]:
    """解析单个 SDF 记录中 ``>  <NAME>`` 数据字段（mol block 之外）。"""
    fields: dict[str, str] = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(">"):
            name = line.split("<", 1)[1].split(">", 1)[0]
            values: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != "":
                values.append(lines[i])
                i += 1
            fields[name] = "\n".join(values)
        i += 1
    return fields


def verify_sdf(reaction: Reaction, mapped_sdf_path: str) -> dict:
    """核验 mapped SDF：记录顺序、SIDE/MOLECULE_INDEX、map 编号与 RXN 对齐。"""
    with open(mapped_sdf_path, encoding="utf-8") as handle:
        blocks = _split_mol_blocks_sdf(handle.read())
    expected = [("reactant", m) for m in reaction.reactants] + \
        [("product", m) for m in reaction.products]
    if len(blocks) != len(expected):
        raise AssertionError("SDF 记录数与 RXN 组件数不一致")
    reactant_maps: list[int] = []
    product_maps: list[int] = []
    for block, (side, mol) in zip(blocks, expected):
        rd_mol = Chem.MolFromMolBlock(block, sanitize=False, removeHs=False,
                                      strictParsing=False)
        if rd_mol is None:
            raise AssertionError("SDF 中存在无法解析的 mol block")
        fields = _sdf_fields(block)
        if "SIDE" not in fields or "MOLECULE_INDEX" not in fields:
            raise AssertionError("SDF 记录缺少 SIDE/MOLECULE_INDEX 字段")
        sdf_side, sdf_index = fields["SIDE"], int(fields["MOLECULE_INDEX"])
        if sdf_side != side or sdf_index != mol.molecule_index:
            raise AssertionError(
                f"SDF 字段 SIDE/MOLECULE_INDEX 错位："
                f"{sdf_side}/{sdf_index} 应为 {side}/{mol.molecule_index}")
        if rd_mol.GetNumAtoms() != len(mol.atoms):
            raise AssertionError("SDF 原子数与 RXN 组件不一致")
        maps = [a.GetAtomMapNum() for a in rd_mol.GetAtoms()]
        if side == "reactant":
            reactant_maps.extend(maps)
        else:
            product_maps.extend(maps)
    n = len(reactant_maps)
    if sorted(reactant_maps) != list(range(1, n + 1)) or \
            sorted(product_maps) != list(range(1, n + 1)):
        raise AssertionError("SDF map 编号不是 1..N 每侧各一次")
    return {"mapped_sdf": mapped_sdf_path, "records": len(blocks),
            "map_numbers_1_to_n_each_side": True}
