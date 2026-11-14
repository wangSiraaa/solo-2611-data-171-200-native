"""写出带 atom-map 的 RXN/SDF。

在原始 mol block 的 RDKit 解析副本上只写 atom-map 编号，
其余原子属性（同位素、形式电荷、价态字段等）、键结构、组件顺序、
坐标与组件名均随 RDKit 原样序列化保留。map 编号为 1..N，
两侧每个编号恰好出现一次。
"""

from __future__ import annotations

from rdkit import Chem

from .models import Molecule, Reaction
from .solver import Mapping


def _mapped_mol_block(mol: Molecule, side_global_to_map: dict[int, int]) -> str:
    # 用带 sanitize 的解析副本写 map：原子顺序与原始解析一致，同位素、
    # 形式电荷、显式价态等属性同源；且只有 sanitize 后的价态缓存能让
    # RDKit 在重写 mol block 时保留原隐式 H 语义（如 OH- 的显式价态字段）。
    rd_mol = Chem.MolFromMolBlock(
        mol.mol_block, removeHs=False, strictParsing=False
    )
    if rd_mol is None:
        raise ValueError(f"{mol.side}:{mol.molecule_index} 无法重新解析")
    # side_global_to_map 以组件内原子索引为键（调用方按组件换算偏移）
    for atom_index in range(rd_mol.GetNumAtoms()):
        rd_mol.GetAtomWithIdx(atom_index).SetAtomMapNum(
            side_global_to_map[atom_index]
        )
    block = Chem.MolToMolBlock(rd_mol, includeStereo=False)
    # 保留原组件名作为 mol block 首行
    lines = block.splitlines()
    lines[0] = mol.name
    return "\n".join(lines) + "\n"


def _global_maps(reaction: Reaction, mapping: Mapping) -> tuple[dict, dict]:
    reactant_map = {r: r + 1 for r, _ in mapping.pairs}
    product_map = {p: r + 1 for r, p in mapping.pairs}
    return reactant_map, product_map


def write_rxn(reaction: Reaction, mapping: Mapping, path: str) -> None:
    r_map, p_map = _global_maps(reaction, mapping)
    blocks: list[str] = []
    for mol in reaction.reactants:
        offset = sum(len(m.atoms) for m in reaction.reactants[:mol.molecule_index])
        local = {i: r_map[offset + i] for i in range(len(mol.atoms))}
        blocks.append(_mapped_mol_block(mol, local))
    for mol in reaction.products:
        offset = sum(len(m.atoms) for m in reaction.products[:mol.molecule_index])
        local = {i: p_map[offset + i] for i in range(len(mol.atoms))}
        blocks.append(_mapped_mol_block(mol, local))

    header = (
        "$RXN\n\n"
        "      RDKit\n\n"
        f"{len(reaction.reactants):3d}{len(reaction.products):3d}\n"
    )
    body = "".join(f"$MOL\n{block}" for block in blocks)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(header + body)


def write_sdf(reaction: Reaction, mapping: Mapping, path: str) -> None:
    r_map, p_map = _global_maps(reaction, mapping)
    records: list[str] = []
    for side, molecules, side_map in (
        ("reactant", reaction.reactants, r_map),
        ("product", reaction.products, p_map),
    ):
        for mol in molecules:
            offset = sum(
                len(m.atoms) for m in molecules[:mol.molecule_index]
            )
            local = {i: side_map[offset + i] for i in range(len(mol.atoms))}
            block = _mapped_mol_block(mol, local)
            record = (
                f"{block}"
                f">  <SIDE>  \n{side}\n\n"
                f">  <MOLECULE_INDEX>  \n{mol.molecule_index}\n\n"
                "$$$$\n"
            )
            records.append(record)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("".join(records))
