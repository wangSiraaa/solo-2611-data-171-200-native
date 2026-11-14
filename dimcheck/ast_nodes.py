"""表达式与语句的语法树节点；每个节点都带 source span。"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .sources import Span

@dataclass(frozen=True)
class Node:
    span: Span


@dataclass(frozen=True)
class Number(Node):
    value: Fraction


@dataclass(frozen=True)
class Var(Node):
    name: str


@dataclass(frozen=True)
class Unary(Node):
    op: str  # "+" 或 "-"
    operand: Node


@dataclass(frozen=True)
class Binary(Node):
    op: str  # "+" "-" "*" "/"
    left: Node
    right: Node


@dataclass(frozen=True)
class Power(Node):
    base: Node
    exponent: Fraction
    exponent_span: Span


@dataclass(frozen=True)
class Call(Node):
    name: str
    args: tuple[Node, ...]


@dataclass(frozen=True)
class FormulaStmt:
    """label : lhs = rhs，一条命名公式。"""

    label: str
    label_span: Span
    lhs: Node
    rhs: Node
    span: Span


@dataclass(frozen=True)
class DeclStmt:
    """name : dimension_product，一条量纲声明。"""

    name: str
    name_span: Span
    span: Span
    dimension: tuple[Fraction, Fraction, Fraction, Fraction]
    dimension_span: Span
