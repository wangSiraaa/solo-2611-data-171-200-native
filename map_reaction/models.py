"""来源可追溯的中间数据模型。

所有原子都带有 ``(side, molecule_index, atom_index)`` 三元组来源标识；
不识别、不丢弃任何重原子，预检在全部读取完成之后进行。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AtomRef:
    """原子来源坐标：侧别 + 组件 0 基索引 + 组件内 0 基原子索引。"""

    side: str  # "reactant" | "product"
    molecule_index: int
    atom_index: int

    def as_string(self) -> str:
        return f"{self.side}:{self.molecule_index}:a{self.atom_index}"


@dataclass
class Atom:
    """一个重原子的全部输入属性（坐标仅作显示，不进入模型决策）。"""

    ref: AtomRef
    atomic_number: int
    symbol: str
    isotope: int  # 0 表示天然同位素
    formal_charge: int
    explicit_hs: int  # 显式 H 邻居数
    implicit_hs: int  # RDKit 按价态推算的隐式 H
    x: float
    y: float
    z: float

    @property
    def invariant(self) -> tuple[int, int]:
        """映射必须保持的不变量：(原子序数, 同位素)。"""
        return (self.atomic_number, self.isotope)


@dataclass
class Edge:
    """两个全局原子序号之间的键，键级只允许 1/2/3。"""

    a: int
    b: int
    order: int

    def pair(self) -> frozenset[int]:
        return frozenset((self.a, self.b))


@dataclass
class Molecule:
    """一个 RXN/SDF 组件（一个 mol block）。"""

    name: str
    side: str
    molecule_index: int
    atoms: list[Atom] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    # 原始 mol block 文本，输出时只在其副本上写 atom-map，
    # 从而保留原组件顺序、原子属性与键结构。
    mol_block: str = ""


@dataclass
class Reaction:
    """一次 RXN 的全部组件，按侧别/组件/原子索引保留来源。"""

    reactants: list[Molecule]
    products: list[Molecule]
    source_path: str

    def molecules(self, side: str) -> list[Molecule]:
        return self.reactants if side == "reactant" else self.products

    def atoms(self, side: str) -> list[Atom]:
        return [a for m in self.molecules(side) for a in m.atoms]

    def bond_order_map(self, side: str) -> dict[frozenset[int], int]:
        """侧内全局原子对 -> 键级；未成键的对不出现在字典中（按 0 处理）。

        键的端点存的是组件内局部索引，这里按各组件原子数累加偏移，
        换算成侧内全局索引，避免多组件的局部索引互相冲突。
        """
        result: dict[frozenset[int], int] = {}
        offset = 0
        for mol in self.molecules(side):
            for edge in mol.edges:
                result[frozenset((offset + edge.a, offset + edge.b))] = edge.order
            offset += len(mol.atoms)
        return result
