"""DISTINCT ON 选行语义的静态校验。

PostgreSQL 要求 DISTINCT ON 的分组键必须是 ORDER BY 的最左前缀；在此之上，
scope.json 额外要求 ORDER BY 的其余键中出现 event_id（主键，全局唯一），
使同组同 tick 这类并列也能唯一选行。本模块只做检查与证据记录，绝不向查询
追加/修改任何排序键——不满足即拒绝该报表。
"""

from __future__ import annotations

from . import ast_nodes as ast
from .errors import CompileError
from .types import TableSchema


def _expr_key(n: ast.Expr) -> str:
    """结构等价键：只比较 AST 结构与标识符/字面量，不比较源位置。"""
    if isinstance(n, ast.ColumnRef):
        table = n.table.strip('"') if n.table else None
        return f"col:{table or ''}.{n.bare_name}"
    if isinstance(n, ast.IntegerLit):
        return f"int:{n.value}"
    if isinstance(n, ast.StringLit):
        return f"str:{n.text}"
    if isinstance(n, ast.BoolLit):
        return f"bool:{n.value}"
    if isinstance(n, ast.NullLit):
        return "null"
    if isinstance(n, ast.Param):
        return f"param:{n.index}"
    if isinstance(n, ast.UnaryOp):
        return f"u{n.op}({_expr_key(n.operand)})"
    if isinstance(n, ast.BinaryOp):
        return f"b{n.op}({_expr_key(n.left)},{_expr_key(n.right)})"
    if isinstance(n, ast.FuncCall):
        return "fn:" + n.name + "(" + ",".join(_expr_key(a) for a in n.args) + ")"
    if isinstance(n, ast.CastExpr):
        return f"cast({_expr_key(n.operand)} AS {n.type_name})"
    return "expr:" + str(id(n))  # 复杂结构保守视为不等


def check_distinct_on(sel: ast.Select, schema: TableSchema,
                      cte_name: str | None) -> list[str]:
    """返回确定性证据（字符串列表）；不满足时抛 CompileError。"""
    if not sel.distinct_on:
        return []
    if not sel.order_by:
        raise CompileError(
            "invalid_ordering",
            "DISTINCT ON 必须配合 ORDER BY：缺少 ORDER BY 时选行不确定",
            sel.src_pos)

    evidence: list[str] = []
    n_keys = len(sel.distinct_on)
    if len(sel.order_by) < n_keys:
        raise CompileError(
            "invalid_ordering",
            f"DISTINCT ON 有 {n_keys} 个分组键，但 ORDER BY 只有 "
            f"{len(sel.order_by)} 项，分组键必须构成 ORDER BY 的最左前缀",
            sel.order_by[-1].src_pos if sel.order_by else sel.src_pos)

    # 1) 分组键 == ORDER BY 最左前缀（结构等价）
    for k_expr, oi in zip(sel.distinct_on, sel.order_by[:n_keys]):
        if _expr_key(k_expr) != _expr_key(oi.expr):
            raise CompileError(
                "invalid_ordering",
                "DISTINCT ON 表达式必须与 ORDER BY 的最左表达式逐项匹配，"
                "否则 PostgreSQL 本身即拒绝该查询",
                oi.src_pos)
    evidence.append(
        "ORDER BY 前 " + str(n_keys) +
        " 项与 DISTINCT ON 分组键逐项相同（结构等价），构成合法最左前缀")

    # 2) 前缀之后必须出现 event_id，保证并列选行唯一
    tiebreakers = sel.order_by[n_keys:]
    event_orders = [oi for oi in tiebreakers
                    if isinstance(oi.expr, ast.ColumnRef)
                    and oi.expr.bare_name == "event_id"]
    if not event_orders:
        raise CompileError(
            "nondeterministic_distinct",
            "ORDER BY 在 DISTINCT ON 分组键之后不含 event_id：同组并列行"
            "（例如同 customer_id 且同 tick 的多行）选行不确定；按 scope "
            "要求拒绝，且不会自行追加排序键改变源查询",
            tiebreakers[-1].src_pos if tiebreakers else sel.src_pos)
    if "event_id" not in schema.types:
        raise CompileError(
            "nondeterministic_distinct",
            f"关系 {schema.name} 不存在 event_id 列，无法保证唯一选行",
            event_orders[0].src_pos)
    oi = event_orders[0]
    evidence.append(
        f"分组键之后的 ORDER BY 含 {schema.name}.event_id"
        f"（{oi.direction}{' NULLS ' + oi.nulls if oi.nulls else ''}），"
        "event_id 为主键且全局唯一，并列行可唯一确定")
    evidence.append(
        "编译器未新增任何排序项，仅复用源 ORDER BY 做窗口分区与最终展示排序")
    return evidence
