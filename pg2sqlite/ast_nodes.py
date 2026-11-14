"""受限 SELECT 方言的 AST 节点。

每个节点都带 src_pos（起始 token 的源位置），用于不支持语法的原位报告。
节点 identity 由对象 id 决定，代码生成阶段借此把表达式在不同作用域间对应起来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .errors import Position


@dataclass
class Node:
    src_pos: Position = field(kw_only=True, default=None)  # type: ignore[assignment]


# ---------- 表达式 ----------------------------------------------------------

@dataclass
class ColumnRef(Node):
    table: Optional[str]   # 限定词（可能带引号），未限定为 None
    name: str              # 列名（带引号时保留双引号形式）

    @property
    def bare_name(self) -> str:
        return self.name.strip('"')


@dataclass
class IntegerLit(Node):
    value: int


@dataclass
class StringLit(Node):
    text: str  # 含外层单引号的原文，保证 '$1' 这样的内容不被参数化


@dataclass
class NullLit(Node):
    pass


@dataclass
class BoolLit(Node):
    value: bool


@dataclass
class Param(Node):
    index: int  # $n 中的 n（从 1 开始）


@dataclass
class UnaryOp(Node):
    op: str            # '-' / 'NOT'
    operand: "Expr"


@dataclass
class BinaryOp(Node):
    op: str
    left: "Expr"
    right: "Expr"


@dataclass
class IsDistinctFrom(Node):
    left: "Expr"
    right: "Expr"
    negated: bool = False  # IS NOT DISTINCT FROM


@dataclass
class IsNull(Node):
    operand: "Expr"
    negated: bool = False


@dataclass
class InList(Node):
    operand: "Expr"
    items: list["Expr"]
    negated: bool = False


@dataclass
class Between(Node):
    operand: "Expr"
    low: "Expr"
    high: "Expr"
    negated: bool = False


@dataclass
class Like(Node):
    operand: "Expr"
    pattern: "Expr"
    negated: bool = False
    ilike: bool = False


@dataclass
class FuncCall(Node):
    name: str
    args: list["Expr"]
    distinct: bool = False


@dataclass
class CastExpr(Node):
    operand: "Expr"
    type_name: str


@dataclass
class CaseExpr(Node):
    operand: Optional["Expr"]
    whens: list[tuple["Expr", "Expr"]]
    default: Optional["Expr"]


Expr = (ColumnRef | IntegerLit | StringLit | NullLit | BoolLit | Param |
        UnaryOp | BinaryOp | IsDistinctFrom | IsNull | InList | Between |
        Like | FuncCall | CastExpr | CaseExpr)


# ---------- 排序与 SELECT ---------------------------------------------------

@dataclass
class OrderItem(Node):
    expr: Expr
    direction: str          # "ASC" / "DESC"
    nulls: Optional[str]    # "FIRST" / "LAST" / None（未显式指定）


@dataclass
class SelectItem(Node):
    expr: Expr
    alias: Optional[str]    # 已去引号的别名；无别名为 None


@dataclass
class Cte(Node):
    name: str
    columns: Optional[list[str]]  # WITH c(...) 形式的输出列名（可选）
    query: "Select"


@dataclass
class Select(Node):
    cte: Optional[Cte]
    distinct_on: list[Expr]       # 空列表表示普通 SELECT
    items: list[SelectItem]
    table: Optional[str]          # 仅支持单表 / 单个 CTE
    where: Optional[Expr]
    order_by: list[OrderItem]
