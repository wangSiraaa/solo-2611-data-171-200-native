"""把声明与公式 AST 编译成四维有理线性约束。

每个表达式节点绑定一个未知量纲向量（命名变量复用全局同名向量，
临时节点取 ``t{n}`` 向量），所有关系都是形如 ``sum a_ij * x_j = b`` 的
线性约束，系数为精确 :class:`fractions.Fraction`。循环公式自然变成
联立约束，不存在依赖环拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from .ast_nodes import (
    Binary,
    Call,
    DeclStmt,
    FormulaStmt,
    Node,
    Number,
    Power,
    Unary,
    Var,
)
from .diagnostics import DiagnosticBag, Severity
from .dimensions import DIMENSIONLESS, FUNCTIONS
from .sources import SourceStore, Span

# 约束种类（均为逐分量的 4 条标量行）：
#   equal    : a = b          （等号、加减相容性、一元符号）
#   add      : a = b + c      （乘法）
#   sub      : a = b - c      （除法）
#   scale    : a = q * b      （有理字面量幂、sqrt）
#   declare  : a = d          （声明，d 为已知四维向量）
#   scalar_in: a = 0          （exp/log/sin/cos 的输入必须无量纲）


@dataclass(frozen=True)
class PrimConstraint:
    kind: str
    item_id: str
    description: str
    span: Span
    terms: tuple[tuple[str, Fraction], ...]  # (变量名, 系数)
    rhs: tuple[Fraction, Fraction, Fraction, Fraction]
    # 幂约束记录指数，供 explain 复核；其余场景为 None
    exponent: Fraction | None = None


@dataclass
class Item:
    item_id: str
    kind: str  # "declaration" | "formula"
    name: str
    file: str
    span: Span
    stmt: object
    accepted: bool = True
    reason: str | None = None
    constraint_ids: list[int] = field(default_factory=list)


@dataclass
class VariableInfo:
    name: str
    is_temp: bool
    spans: list[Span] = field(default_factory=list)


@dataclass
class CompiledSystem:
    items: list[Item]
    constraints: list[PrimConstraint]
    variables: list[str]                 # 实变量在前、临时变量在后，顺序稳定
    variable_spans: dict[str, list[Span]]
    temp_of_node: dict[int, str]


class Compiler:
    def __init__(self, sources: SourceStore, diags: DiagnosticBag) -> None:
        self.sources = sources
        self.diags = diags
        self.items: list[Item] = []
        self.constraints: list[PrimConstraint] = []
        self._real_order: list[str] = []
        self._temp_order: list[str] = []
        self._spans: dict[str, list[Span]] = {}
        self._temp_count = 0
        self._node_temps: dict[int, str] = {}

    # ---- 变量表 -------------------------------------------------------

    def _real(self, name: str, span: Span) -> str:
        if name not in self._spans:
            self._real_order.append(name)
            self._spans[name] = []
        self._spans[name].append(span)
        return name

    def _temp(self, node: Node) -> str:
        key = id(node)
        if key not in self._node_temps:
            name = f"t{self._temp_count}"
            self._temp_count += 1
            self._temp_order.append(name)
            self._node_temps[key] = name
            self._spans[name] = [node.span]
        return self._node_temps[key]

    def _add(self, con: PrimConstraint, item: Item) -> None:
        cid = len(self.constraints)
        self.constraints.append(con)
        item.constraint_ids.append(cid)

    # ---- 入口 ---------------------------------------------------------

    def compile(
        self,
        declarations: list[DeclStmt],
        formulas: list[FormulaStmt],
        file_path: str,
    ) -> None:
        for decl in declarations:
            self._compile_decl(decl, file_path)
        for formula in formulas:
            self._compile_formula(formula, file_path)

    def _compile_decl(self, decl: DeclStmt, file_path: str) -> None:
        item = Item(
            item_id=f"{file_path}#decl#{decl.name}",
            kind="declaration",
            name=decl.name,
            file=file_path,
            span=decl.span,
            stmt=decl,
        )
        self.items.append(item)
        var = self._real(decl.name, decl.name_span)
        self._add(
            PrimConstraint(
                kind="declare",
                item_id=item.item_id,
                description=f"声明 {decl.name} 的量纲 = "
                f"{_fmt_vec(decl.dimension)}",
                span=decl.dimension_span,
                terms=((var, Fraction(1)),),
                rhs=tuple(Fraction(x) for x in decl.dimension),
            ),
            item,
        )

    def _compile_formula(self, formula: FormulaStmt, file_path: str) -> None:
        item = Item(
            item_id=f"{file_path}#formula#{formula.label}",
            kind="formula",
            name=formula.label,
            file=file_path,
            span=formula.span,
            stmt=formula,
        )
        self.items.append(item)
        try:
            lhs = self._compile_expr(formula.lhs, item)
            rhs = self._compile_expr(formula.rhs, item)
        except _RejectStatement as reject:
            item.accepted = False
            item.reason = reject.code
            self.diags.add(
                reject.code,
                reject.message,
                reject.span,
                Severity.SEMANTIC_ERROR,
                self.sources.snippet(reject.span),
            )
            return
        self._add(
            PrimConstraint(
                kind="equal",
                item_id=item.item_id,
                description=f"公式 {formula.label} 等号两侧量纲相等",
                span=formula.span,
                terms=((lhs, Fraction(1)), (rhs, Fraction(-1))),
                rhs=DIMENSIONLESS,
            ),
            item,
        )

    # ---- 表达式 -------------------------------------------------------

    def _compile_expr(self, node: Node, item: Item) -> str:
        if isinstance(node, Number):
            name = self._temp(node)
            # 纯数（含 0）恒为无量纲；不存在多态的量纲零。
            self._add(
                PrimConstraint(
                    kind="declare",
                    item_id=item.item_id,
                    description=f"字面量 {node.value} 是无量纲纯数",
                    span=node.span,
                    terms=((name, Fraction(1)),),
                    rhs=DIMENSIONLESS,
                ),
                item,
            )
            return name

        if isinstance(node, Var):
            return self._real(node.name, node.span)

        if isinstance(node, Unary):
            inner = self._compile_expr(node.operand, item)
            name = self._temp(node)
            sign = "+" if node.op == "+" else "-"
            self._add(
                PrimConstraint(
                    kind="equal",
                    item_id=item.item_id,
                    description=f"一元 {sign} 不改变量纲",
                    span=node.span,
                    terms=((name, Fraction(1)), (inner, Fraction(-1))),
                    rhs=DIMENSIONLESS,
                ),
                item,
            )
            return name

        if isinstance(node, Binary):
            left = self._compile_expr(node.left, item)
            right = self._compile_expr(node.right, item)
            name = self._temp(node)
            if node.op in ("+", "-"):
                sym = "加" if node.op == "+" else "减"
                # 加减：两侧维数必须相等，结果维数与两侧一致。
                self._add(
                    PrimConstraint(
                        kind="equal",
                        item_id=item.item_id,
                        description=f"{sym}法两侧量纲相容",
                        span=node.span,
                        terms=((left, Fraction(1)), (right, Fraction(-1))),
                        rhs=DIMENSIONLESS,
                    ),
                    item,
                )
                self._add(
                    PrimConstraint(
                        kind="equal",
                        item_id=item.item_id,
                        description=f"{sym}法结果取相同量纲",
                        span=node.span,
                        terms=((name, Fraction(1)), (left, Fraction(-1))),
                        rhs=DIMENSIONLESS,
                    ),
                    item,
                )
            elif node.op == "*":
                self._add(
                    PrimConstraint(
                        kind="add",
                        item_id=item.item_id,
                        description="乘法量纲相加",
                        span=node.span,
                        terms=(
                            (name, Fraction(1)),
                            (left, Fraction(-1)),
                            (right, Fraction(-1)),
                        ),
                        rhs=DIMENSIONLESS,
                    ),
                    item,
                )
            else:  # "/"
                self._add(
                    PrimConstraint(
                        kind="sub",
                        item_id=item.item_id,
                        description="除法量纲相减",
                        span=node.span,
                        terms=(
                            (name, Fraction(1)),
                            (left, Fraction(-1)),
                            (right, Fraction(1)),
                        ),
                        rhs=DIMENSIONLESS,
                    ),
                    item,
                )
            return name

        if isinstance(node, Power):
            base = self._compile_expr(node.base, item)
            name = self._temp(node)
            self._add(
                PrimConstraint(
                    kind="scale",
                    item_id=item.item_id,
                    description=f"有理字面量幂：维数乘 {node.exponent}",
                    span=node.span,
                    terms=((name, Fraction(1)), (base, -node.exponent)),
                    rhs=DIMENSIONLESS,
                    exponent=Fraction(node.exponent),
                ),
                item,
            )
            return name

        if isinstance(node, Call):
            return self._compile_call(node, item)

        raise AssertionError(f"未知 AST 节点 {type(node)!r}")  # pragma: no cover

    def _compile_call(self, node: Call, item: Item) -> str:
        spec = FUNCTIONS.get(node.name)
        if spec is None:
            raise _RejectStatement(
                "E300",
                f"未知函数 {node.name!r}；受限函数表仅含 "
                "exp/log/sin/cos/sqrt",
                node.span,
            )
        arity, kind = spec
        if len(node.args) != arity:
            raise _RejectStatement(
                "E301",
                f"函数 {node.name} 接受 {arity} 个参数，得到 {len(node.args)} 个",
                node.span,
            )
        arg = self._compile_expr(node.args[0], item)
        name = self._temp(node)
        if kind == "scalar":
            # 输入与输出均无量纲。
            self._add(
                PrimConstraint(
                    kind="scalar_in",
                    item_id=item.item_id,
                    description=f"{node.name} 的输入必须无量纲",
                    span=node.span,
                    terms=((arg, Fraction(1)),),
                    rhs=DIMENSIONLESS,
                ),
                item,
            )
            self._add(
                PrimConstraint(
                    kind="declare",
                    item_id=item.item_id,
                    description=f"{node.name} 的输出无量纲",
                    span=node.span,
                    terms=((name, Fraction(1)),),
                    rhs=DIMENSIONLESS,
                ),
                item,
            )
        else:  # sqrt
            self._add(
                PrimConstraint(
                    kind="scale",
                    item_id=item.item_id,
                    description="sqrt：输出量纲 = 输入量纲 * 1/2",
                    span=node.span,
                    terms=((name, Fraction(1)), (arg, Fraction(-1, 2))),
                    rhs=DIMENSIONLESS,
                    exponent=Fraction(1, 2),
                ),
                item,
            )
        return name

    # ---- 结果 ---------------------------------------------------------

    def result(self) -> CompiledSystem:
        return CompiledSystem(
            items=self.items,
            constraints=self.constraints,
            variables=self._real_order + self._temp_order,
            variable_spans=self._spans,
            temp_of_node=self._node_temps,
        )


class _RejectStatement(Exception):
    def __init__(self, code: str, message: str, span: Span) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.span = span


def _fmt_vec(vec) -> str:
    from .dimensions import format_dimension

    return format_dimension(vec)
