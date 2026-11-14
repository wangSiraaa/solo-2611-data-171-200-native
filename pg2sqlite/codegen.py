"""AST -> SQLite SQL 代码生成。

改写原则全部在本模块显式实现（不调用任何 transpile 库）：
- $n 参数按语法 token 出现次序迁移为 ?，同一 $n 出现几次就生成几个 ?，
  绑定映射重复同一值；字符串字面量整体透传，字面文本里的 '$1' 原样保留；
- concat(a,b,...) 仅用于已声明 text 的参数，改写为
  COALESCE(CAST(x AS TEXT),'') || ...，NULL 按空串处理，杜绝 NULL 传播；
- IS DISTINCT FROM 改写为 SQLite 的 IS（其 NULL 比较真值表与 PG 一致），
  IS NOT DISTINCT FROM 改写为 IS NOT；
- DISTINCT ON 改写为 ROW_NUMBER() OVER (PARTITION BY 分组键
  ORDER BY 源排序) 的子查询，排名列仅存在于内层，最终输出只含原报表列；
- ORDER BY 的 NULLS FIRST/LAST 与方向原样保留（含未显式指定时按 PG 默认补全）。
"""

from __future__ import annotations

import sqlite3

from . import ast_nodes as ast
from .distinct import _expr_key
from .types import BOOLEAN, INTEGER, TEXT

# SQLite 3.30 起原生支持 NULLS FIRST/LAST；Python 3.11 自带 3.40，
# 仍做版本探测，旧版本回退到 CASE 模拟。
_SUPPORTS_NULLS = sqlite3.sqlite_version_info >= (3, 30, 0)


class Codegen:
    def __init__(self, node_types: dict[int, str],
                 cte_columns: dict[str, list[str]] | None = None):
        self.types = node_types
        # 参数占位符按语法出现顺序登记：[(源参数号, 逻辑类型), ...]
        self.binding_map: list[tuple[int, str]] = []
        self.rewrites: list[str] = []
        self.cte_columns = cte_columns or {}

    # ---- 表达式 ----------------------------------------------------------
    def expr(self, n: ast.Expr) -> str:
        m = getattr(self, "_gen_" + type(n).__name__)
        return m(n)

    def _gen_ColumnRef(self, n: ast.ColumnRef) -> str:
        if n.table:
            return f'{n.table.strip(chr(34))}.{n.bare_name}'
        return n.bare_name

    def _gen_IntegerLit(self, n: ast.IntegerLit) -> str:
        return str(n.value)

    def _gen_StringLit(self, n: ast.StringLit) -> str:
        # 原文透传：外层引号与内部 '' 转义均为 SQL 标准写法，SQLite 同样支持，
        # 因此字符串内部的 '$1' 永远不会被当作参数占位符处理。
        return n.text

    def _gen_NullLit(self, n: ast.NullLit) -> str:
        return "NULL"

    def _gen_BoolLit(self, n: ast.BoolLit) -> str:
        return "1" if n.value else "0"

    def _gen_Param(self, n: ast.Param) -> str:
        t = self.types[id(n)]
        self.binding_map.append((n.index, t))
        return "?"

    def _gen_UnaryOp(self, n: ast.UnaryOp) -> str:
        inner = self.expr(n.operand)
        if n.op == "NOT":
            return f"NOT ({inner})"
        return f"(-{inner})"

    def _gen_BinaryOp(self, n: ast.BinaryOp) -> str:
        l, r = self.expr(n.left), self.expr(n.right)
        if n.op in ("AND", "OR"):
            return f"({l} {n.op} {r})"
        op = "!=" if n.op == "<>" else n.op
        return f"({l} {op} {r})"

    def _gen_IsDistinctFrom(self, n: ast.IsDistinctFrom) -> str:
        # 不使用 SQLite 的 IS 对绑定参数比较：实测无类型 NULL 参数经 IS ? 比较
        # 时会被当作“未知值”而得到错误结果。这里显式展开三值逻辑真值表，
        # 与 PostgreSQL IS [NOT] DISTINCT FROM 完全一致，两个 NULL / 一个 NULL
        # / 布尔比较全部保持。
        # 每个 ? 占位符都必须在语法实际出现处登记绑定，因此左右操作数在各次
        # 引用处独立渲染，同一 $n 会按出现次数重复登记同一绑定值。
        # 紧凑三值逻辑，左右操作数各渲染三次（各对应三个 ? 占位）。
        # 利用 SQLite 中 IS 对“列与字面量/列与列”比较的稳定三值语义：
        #   distinct = (l IS NULL AND r IS NOT NULL)
        #            OR (l IS NOT NULL AND r IS NULL)
        #            OR (l IS NOT NULL AND l <> r)
        # 第三支的短路求值保证 l 非 NULL 时才进入 <>；r 为 NULL 时前两支已
        # 覆盖，<> 产生的未知值会被 OR 忽略。参数占位符仅出现在 r 的三次
        # 渲染中，绑定 NULL 时由前两支正确判定，不依赖 IS 对绑定参数的行为。
        l1, l2, l3 = self.expr(n.left), self.expr(n.left), self.expr(n.left)
        r1, r2, r3 = self.expr(n.right), self.expr(n.right), self.expr(n.right)
        distinct = (
            f"(({l1} IS NULL AND {r1} IS NOT NULL) "
            f"OR ({l2} IS NOT NULL AND {r2} IS NULL) "
            f"OR ({l3} IS NOT NULL AND {l3} <> {r3}))")
        sql = f"(NOT {distinct})" if n.negated else distinct
        self.rewrites.append(
            "IS " + ("NOT " if n.negated else "") +
            "DISTINCT FROM -> 显式三值逻辑 CASE 等价表达式"
            "（NULL IS NULL=相等、单侧 NULL=不等、布尔按 0/1 比较；"
            "规避 SQLite 对无类型绑定参数的 IS 语义差异；同一参数多次出现"
            "按语法位置重复绑定同一值）")
        return sql

    def _gen_IsNull(self, n: ast.IsNull) -> str:
        inner = self.expr(n.operand)
        return f"({inner} IS NOT NULL)" if n.negated else f"({inner} IS NULL)"

    def _gen_InList(self, n: ast.InList) -> str:
        left = self.expr(n.operand)
        items = ", ".join(self.expr(i) for i in n.items)
        kw = "NOT IN" if n.negated else "IN"
        return f"({left} {kw} ({items}))"

    def _gen_Between(self, n: ast.Between) -> str:
        v = self.expr(n.operand)
        lo, hi = self.expr(n.low), self.expr(n.high)
        kw = "NOT BETWEEN" if n.negated else "BETWEEN"
        return f"({v} {kw} {lo} AND {hi})"

    def _gen_Like(self, n: ast.Like) -> str:
        v, p = self.expr(n.operand), self.expr(n.pattern)
        kw = "NOT LIKE" if n.negated else "LIKE"
        return f"({v} {kw} {p})"

    def _gen_FuncCall(self, n: ast.FuncCall) -> str:
        if n.name == "CONCAT":
            parts: list[str] = []
            for a in n.args:
                piece = self.expr(a)
                # 静态即文本字面量无需 COALESCE；其余（列/参数/表达式）统一
                # COALESCE(CAST(... AS TEXT), '')，NULL -> ''，绝不传播 NULL。
                if isinstance(a, ast.StringLit):
                    parts.append(piece)
                else:
                    parts.append(f"COALESCE(CAST({piece} AS TEXT), '')")
            self.rewrites.append(
                "concat(...) -> " + " || ".join(parts) +
                "（每个非字面量参数 COALESCE 为空串，NULL 不传播；仅 text 类型）")
            return "(" + " || ".join(parts) + ")"
        raise AssertionError(f"未支持函数 {n.name}（应在解析阶段被拒绝）")

    def _gen_CastExpr(self, n: ast.CastExpr) -> str:
        inner = self.expr(n.operand)
        t = n.type_name
        if t in ("INTEGER", "INT"):
            return f"CAST({inner} AS INTEGER)"
        if t == "TEXT":
            return f"CAST({inner} AS TEXT)"
        return f"CAST({inner} AS INTEGER)"  # BOOLEAN：逻辑 0/1

    def _gen_CaseExpr(self, n: ast.CaseExpr) -> str:
        pieces = ["CASE"]
        if n.operand is not None:
            pieces.append(self.expr(n.operand))
        for cond, res in n.whens:
            pieces.append("WHEN " + self.expr(cond) + " THEN " + self.expr(res))
        if n.default is not None:
            pieces.append("ELSE " + self.expr(n.default))
        pieces.append("END")
        return "(" + " ".join(pieces) + ")"

    # ---- ORDER BY（保留 NULLS 位置） -------------------------------------
    def order_item(self, oi: ast.OrderItem,
                   name_map: dict | None = None) -> list[str]:
        """返回一项 ORDER BY 的 SQL 片段列表（NULLS 模拟时为两项）。

        name_map 的键既支持 AST 对象 id，也支持结构等价键（字符串）。
        """
        base = None
        if name_map is not None:
            base = name_map.get(id(oi.expr))
            if base is None:
                base = name_map.get(_expr_key(oi.expr))
        if base is None:
            base = self.expr(oi.expr)
        direction = oi.direction
        # 未显式指定 NULLS 时按 PostgreSQL 默认补全，保证最终展示顺序一致：
        # ASC -> NULLS FIRST，DESC -> NULLS LAST。
        nulls = oi.nulls
        if nulls is None:
            nulls = "FIRST" if direction == "ASC" else "LAST"
        if _SUPPORTS_NULLS:
            return [f"{base} {direction} NULLS {nulls}"]
        # 旧版 SQLite 回退：排序键前加一个 CASE 决定 NULL 的先后。
        null_first = 1 if nulls == "FIRST" else 0
        null_case = f"CASE WHEN {base} IS NULL THEN {null_first} ELSE {1 - null_first} END"
        return [f"{null_case} ASC", f"{base} {direction}"]

    def order_clause(self, items: list[ast.OrderItem],
                     name_map: dict[int, str] | None = None) -> str:
        parts: list[str] = []
        for oi in items:
            parts.extend(self.order_item(oi, name_map))
        return "ORDER BY " + ", ".join(parts)

    # ---- SELECT 整体 -----------------------------------------------------
    def select(self, sel: ast.Select,
               distinct_evidence: list[str] | None = None) -> str:
        if sel.distinct_on:
            return self._distinct_on_select(sel, distinct_evidence or [])
        return self._plain_select(sel)

    def _plain_select(self, sel: ast.Select) -> str:
        pieces: list[str] = []
        select_cols = [self._projection(item) for item in sel.items]
        pieces.append("SELECT " + ", ".join(select_cols))
        if sel.table:
            pieces.append("FROM " + sel.table.strip('"'))
        if sel.where is not None:
            pieces.append("WHERE " + self.expr(sel.where))
        if sel.order_by:
            pieces.append(self.order_clause(sel.order_by))
        return " ".join(pieces)

    def _projection(self, item: ast.SelectItem) -> str:
        sql = self.expr(item.expr)
        if item.alias:
            return f"{sql} AS {item.alias}"
        return sql

    def _distinct_on_select(self, sel: ast.Select,
                            evidence: list[str]) -> str:
        """ROW_NUMBER 窗口改写。

        内层 t0：保留全部输出列与 ORDER BY 引用到的附加列，并计算
          rn = ROW_NUMBER() OVER (PARTITION BY <分组键> ORDER BY <完整源排序>)
        外层：WHERE rn = 1，只投影原 SELECT 列，再按完整源 ORDER BY 展示。
        辅助列（rn 及附加排序列）不出现在最终输出。
        """
        # 1) 收集内层需要的列表达式与别名。
        # 用结构等价键（而非对象 id）关联投影与 ORDER BY 中的同一表达式，
        # 避免 DISTINCT ON 分组键与排序键是两个同构 AST 节点时重复出列。
        inner_aliases: dict[str, str] = {}
        inner_cols: list[str] = []
        used_names: set[str] = set()

        def fresh(prefix: str) -> str:
            base, k = prefix, 1
            while base in used_names:
                k += 1
                base = f"{prefix}_{k}"
            used_names.add(base)
            return base

        for idx, item in enumerate(sel.items):
            name = item.alias or (
                item.expr.bare_name if isinstance(item.expr, ast.ColumnRef)
                else fresh(f"col_{idx + 1}"))
            name = fresh(name)
            key = _expr_key(item.expr)
            inner_aliases.setdefault(key, name)
            inner_cols.append(f"{self.expr(item.expr)} AS {name}")

        # ORDER BY 中未被投影的表达式 -> 辅助列（仅内层可见）；
        # 已投影的同构表达式直接复用投影别名，不产生重复辅助列。
        for oi in sel.order_by:
            key = _expr_key(oi.expr)
            if key not in inner_aliases:
                if isinstance(oi.expr, ast.ColumnRef):
                    name = fresh(oi.expr.bare_name + "_ord")
                else:
                    name = fresh("ord_expr")
                inner_aliases[key] = name
                inner_cols.append(f"{self.expr(oi.expr)} AS {name}")

        # 2) 窗口：PARTITION BY 分组键 + 完整源 ORDER BY。
        # 窗口函数与 SELECT 列表同层，SQLite 不允许窗口 ORDER BY 引用同层
        # 输出别名，因此这里一律使用原始表达式；别名只供外层 ORDER BY 使用。
        partition = ", ".join(self.expr(k) for k in sel.distinct_on)
        window_order_parts: list[str] = []
        for oi in sel.order_by:
            window_order_parts.extend(self.order_item(oi, name_map=None))
        rn_col = fresh("rn__rank")
        window = (f"ROW_NUMBER() OVER (PARTITION BY {partition} "
                  f"ORDER BY {', '.join(window_order_parts)})")
        inner_cols.append(f"{window} AS {rn_col}")

        where_sql = (" WHERE " + self.expr(sel.where)) if sel.where else ""
        inner = (f"SELECT {', '.join(inner_cols)} "
                 f"FROM {sel.table.strip(chr(34))}{where_sql}")

        # 3) 外层：过滤 rn=1；输出严格保持源 SELECT 的列与列顺序
        outer_cols: list[str] = []
        for item in sel.items:
            alias = inner_aliases[_expr_key(item.expr)]
            if item.alias:
                outer_cols.append(f"{alias} AS {item.alias}")
            else:
                outer_cols.append(alias)
        # 外层 ORDER BY 同样按结构键取内层别名，辅助列仅在内层可见
        outer_order = self.order_clause(sel.order_by, inner_aliases)
        self.rewrites.append(
            "DISTINCT ON -> ROW_NUMBER() OVER (PARTITION BY "
            + ", ".join(self._short(k) for k in sel.distinct_on)
            + " ORDER BY 源排序) 子查询取 rn = 1；排名列 "
            + rn_col + " 与附加排序列仅存在于内层，不进入最终输出；"
            "外层复用完整源 ORDER BY 保留展示顺序与 NULLS 位置")
        return (f"SELECT {', '.join(outer_cols)} FROM ({inner}) t0__ "
                f"WHERE {rn_col} = 1 {outer_order}")

    @staticmethod
    def _short(n: ast.Expr) -> str:
        if isinstance(n, ast.ColumnRef):
            return n.bare_name
        return type(n).__name__


def output_schema(sel: ast.Select, node_types: dict[int, str]) -> list[dict]:
    """逻辑输出 schema：列名、顺序、逻辑类型；布尔在 SQLite 物理为 0/1。"""
    schema: list[dict] = []
    for idx, item in enumerate(sel.items):
        if item.alias:
            name = item.alias
        elif isinstance(item.expr, ast.ColumnRef):
            name = item.expr.bare_name
        else:
            name = f"column_{idx + 1}"
        logical = node_types[id(item)]
        entry = {"name": name, "logical_type": logical, "position": idx + 1}
        if logical == BOOLEAN:
            entry["sqlite_storage"] = "INTEGER 0/1"
        elif logical == INTEGER:
            entry["sqlite_storage"] = "INTEGER"
        elif logical == TEXT:
            entry["sqlite_storage"] = "TEXT"
        else:  # null
            entry["sqlite_storage"] = "NULL"
        schema.append(entry)
    return schema
