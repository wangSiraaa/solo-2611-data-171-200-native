"""RXN/SDF 读取：真实组件、原子、键、同位素、形式电荷及氢信息。

只用 RDKit 做 mol block 解析与隐式氢推算，组件切分按 RXN/SDF 的
文本行规则自行完成，以确保来源（侧别/组件索引/组件内原子索引）完整保留。
"""

from __future__ import annotations

from rdkit import Chem

from .errors import UnsupportedFixtureError
from .models import Atom, AtomRef, Edge, Molecule, Reaction

_PARSER_KWARGS = dict(sanitize=False, removeHs=False, strictParsing=False)
_SUPPORTED_BOND_TYPES = {
    Chem.BondType.SINGLE: 1,
    Chem.BondType.DOUBLE: 2,
    Chem.BondType.TRIPLE: 3,
}


def _split_mol_blocks_rxn(text: str) -> tuple[int, int, list[str]]:
    """按 ``$MOL`` 切分 RXN，返回 (反应物数, 产物数, 各 mol block 原文)。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "$RXN":
        raise ValueError("不是合法的 V2000 RXN（缺少 $RXN 头）")
    try:
        counts_line = next(line for line in lines[3:] if line.strip())
    except StopIteration as exc:
        raise ValueError("RXN 缺少组件计数行") from exc
    n_reactants = int(counts_line[0:3])
    n_products = int(counts_line[3:6])
    blocks: list[str] = []
    current: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == "$MOL":
            if inside:
                blocks.append("\n".join(current))
            current = []
            inside = True
            continue
        if inside:
            current.append(line)
    if inside:
        blocks.append("\n".join(current))
    if len(blocks) != n_reactants + n_products:
        raise ValueError(
            f"RXN 计数行声称 {n_reactants}+{n_products} 个组件，"
            f"实际解析到 {len(blocks)} 个 $MOL 块"
        )
    return n_reactants, n_products, blocks


def _split_mol_blocks_sdf(text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip() == "$$$$":
            if current:
                blocks.append("\n".join(current))
            current = []
            continue
        current.append(line)
    if current and any(line.strip() for line in current):
        blocks.append("\n".join(current))
    return blocks


def _build_molecule(block: str, side: str, molecule_index: int) -> Molecule:
    raw = Chem.MolFromMolBlock(block, **_PARSER_KWARGS)
    if raw is None:
        raise ValueError(f"{side}:{molecule_index} 的 mol block 无法解析")
    # 再做一次带 sanitize 的解析，仅用于取得隐式氢；原始结构以 raw 为准。
    sanitized = Chem.MolFromMolBlock(block, removeHs=False, strictParsing=False)
    if sanitized is None:
        raise UnsupportedFixtureError(
            f"{side}:{molecule_index} 无法通过价态 sanitize，本工具不支持该结构"
        )

    name = (raw.GetProp("_Name") if raw.HasProp("_Name") else "").strip()
    mol = Molecule(name=name, side=side, molecule_index=molecule_index,
                   mol_block=block)

    # 显式 H 邻居在 sanitized 图上统计（raw 与 sanitized 的原子顺序一致）。
    explicit_h = [
        sum(1 for neighbor in sanitized.GetAtomWithIdx(i).GetNeighbors()
            if neighbor.GetAtomicNum() == 1)
        for i in range(raw.GetNumAtoms())
    ]
    for i, rd_atom in enumerate(raw.GetAtoms()):
        if rd_atom.GetAtomicNum() == 1:
            # 任务范围只有重原子；显式 H 已计入其重原子邻居的 explicit_hs，
            # 这里不进入双射、也不静默“丢掉”——直接报告范围错误。
            raise UnsupportedFixtureError(
                f"{side}:{molecule_index} 含显式氢原子（组件内索引 {i}），"
                "本任务只处理重原子"
            )
        s_atom = sanitized.GetAtomWithIdx(i)
        conformer = raw.GetConformer() if raw.GetNumConformers() else None
        position = conformer.GetAtomPosition(i) if conformer is not None else None
        mol.atoms.append(
            Atom(
                ref=AtomRef(side, molecule_index, i),
                atomic_number=rd_atom.GetAtomicNum(),
                symbol=rd_atom.GetSymbol(),
                isotope=rd_atom.GetIsotope(),
                formal_charge=rd_atom.GetFormalCharge(),
                explicit_hs=explicit_h[i],
                implicit_hs=s_atom.GetNumImplicitHs(),
                x=position.x if position is not None else 0.0,
                y=position.y if position is not None else 0.0,
                z=position.z if position is not None else 0.0,
            )
        )

    for bond in raw.GetBonds():
        order = _SUPPORTED_BOND_TYPES.get(bond.GetBondType())
        if order is None:
            raise UnsupportedFixtureError(
                f"{side}:{molecule_index} 含非单/双/三键"
                f"（组件内键 {bond.GetBeginAtomIdx()}-{bond.GetEndAtomIdx()}，"
                "芳香键等不在支持范围）"
            )
        mol.edges.append(
            Edge(a=bond.GetBeginAtomIdx(), b=bond.GetEndAtomIdx(), order=order)
        )
    return mol


def parse_rxn(path: str) -> Reaction:
    """解析真实 RXN，按侧别/组件/原子索引保留全部来源信息。"""
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    n_reactants, n_products, blocks = _split_mol_blocks_rxn(text)
    reactants = [_build_molecule(block, "reactant", i)
                 for i, block in enumerate(blocks[:n_reactants])]
    products = [_build_molecule(block, "product", i)
                for i, block in enumerate(blocks[n_reactants:])]
    return Reaction(reactants=reactants, products=products, source_path=path)


def _sdf_fields(block: str) -> dict[str, str]:
    """解析单个 SDF 记录中 ``>  <NAME>`` 数据字段（mol block 之外）。"""
    fields: dict[str, str] = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].startswith(">") and "<" in lines[i]:
            name = lines[i].split("<", 1)[1].split(">", 1)[0]
            values: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != "":
                values.append(lines[i])
                i += 1
            fields[name] = "\n".join(values)
        i += 1
    return fields


def parse_sdf(path: str) -> list[Molecule]:
    """解析辅助 SDF；SIDE/MOLECULE_INDEX 字段用于与 RXN 来源对账。"""
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    molecules: list[Molecule] = []
    for block in _split_mol_blocks_sdf(text):
        fields = _sdf_fields(block)
        side = fields.get("SIDE", "")
        index = int(fields["MOLECULE_INDEX"]) \
            if "MOLECULE_INDEX" in fields else len(molecules)
        molecules.append(_build_molecule(block, side, index))
    return molecules
