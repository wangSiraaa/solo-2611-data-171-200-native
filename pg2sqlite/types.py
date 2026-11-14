"""声明类型模型、表 schema 解析与 AST 类型推断/检查。

逻辑类型仅四种：integer / text / boolean / null（NULL 字面量的静态类型，
可与任意列类型兼容）。布尔在 SQLite 物理存为 0/1，但逻辑层始终映回 boolean，
绝不把列整体转成字符串比较。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import ast_nodes as ast
from .errors import CompileError
from .lexer import Lexer, Token

INTEGER = "integer"
TEXT = "text"
BOOLEAN = "boolean"
NULLTYPE = "null"

ALL_TYPES = frozenset({INTEGER, TEXT, BOOLEAN, NULLTYPE})


def compatible(a: str, b: str) -> bool:
    """null 与任意类型兼容；其余必须同名（不做隐式数值/文本转换）。"""
    if a == NULLTYPE or b == NULLTYPE:
        return True
    return a == b


def combine(a: str, b: str) -> str:
    """两兼容类型合并为非空侧类型；两侧都为 null 时保持 null。"""
    if a == NULLTYPE:
        return b
    return a


# ---------------------------------------------------------------------------
# 轻量 DDL 解析：复用主词法分析，仅识别 CREATE TABLE 结构
# ---------------------------------------------------------------------------

@dataclass
class TableSchema:
    name: str
    columns: list[str]
    types: dict[str, str]

    def type_of(self, column: str, pos) -> str:
        if column not in self.types:
            raise CompileError(
                "parse_error", f"表 {self.name} 不存在列 {column}", pos)
        return self.types[column]


def _ddl_type(sql_type: str) -> str:
    t = sql_type.upper()
    if t in ("INTEGER", "INT", "INT4", "INT8", "BIGINT", "SMALLINT",
             "SERIAL", "BIGSERIAL"):
        return INTEGER
    if t in ("TEXT", "VARCHAR", "CHAR", "CHARACTER VARYING", "NAME"):
        return TEXT
    if t in ("BOOLEAN", "BOOL"):
        return BOOLEAN
    raise CompileError("unsupported_syntax",
                       f"列类型 {sql_type} 不在 scope（integer/text/boolean）")


def parse_ddl(ddl: str) -> dict[str, TableSchema]:
    """从 CREATE TABLE 语句集合提取表名、列顺序与逻辑类型。"""
    tokens = Lexer(ddl).tokenize()
    tables: dict[str, TableSchema] = {}
    i = 0
    n = len(tokens)

    def kw(word: str) -> bool:
        return i < n and tokens[i].kind == "KEYWORD" and tokens[i].text == word

    def op(sym: str) -> bool:
        return i < n and tokens[i].kind == "OP" and tokens[i].text == sym

    while i < n:
        if not kw("CREATE"):
            i += 1
            continue
        create_tok = tokens[i]
        i += 1
        if not kw("TABLE"):
            # CREATE INDEX/VIEW 等本 scope 不出现，跳过即可
            continue
        i += 1
        if i >= n or tokens[i].kind != "IDENT":
            raise CompileError("parse_error", "CREATE TABLE 后缺少表名",
                               create_tok.pos, ddl)
        table_name = tokens[i].text.strip('"')
        i += 1
        if not op("("):
            raise CompileError("parse_error", "CREATE TABLE 缺少 (",
                               tokens[i - 1].pos, ddl)
        i += 1
        cols: list[str] = []
        types: dict[str, str] = {}
        while not op(")"):
            if i >= len(tokens):
                raise CompileError("parse_error", "CREATE TABLE 未闭合")
            col_tok = tokens[i]
            if col_tok.kind != "IDENT":
                # 跳过表级约束（PRIMARY KEY(...)、CHECK(...)、UNIQUE(...)）
                if col_tok.kind == "KEYWORD":
                    i = _skip_ddl_constraint(tokens, i)
                    if op(","):
                        i += 1
                    continue
                raise CompileError("parse_error", "DDL 中期望列名",
                                   col_tok.pos, ddl)
            col_name = col_tok.text.strip('"')
            i += 1
            if i >= len(tokens) or tokens[i].kind != "IDENT":
                raise CompileError("parse_error",
                                   f"列 {col_name} 缺少类型声明",
                                   col_tok.pos, ddl)
            cols.append(col_name)
            types[col_name] = _ddl_type(tokens[i].text)
            i += 1
            # 跳过列级约束（PRIMARY KEY / NOT NULL / CHECK(...) 等），
            # 用括号深度跟踪，直到深度为 0 上的逗号或表定义右括号。
            depth = 0
            while i < n:
                t = tokens[i]
                if t.kind == "OP" and t.text == "(":
                    depth += 1
                elif t.kind == "OP" and t.text == ")":
                    if depth == 0:
                        break  # 表定义闭合
                    depth -= 1
                elif depth == 0 and t.kind == "OP" and t.text == ",":
                    break
                i += 1
            if op(","):
                i += 1
        i += 1  # )
        tables[table_name] = TableSchema(table_name, cols, types)
    return tables


def _skip_ddl_constraint(tokens: list[Token], i: int) -> int:
    depth = 0
    while i < len(tokens):
        t = tokens[i]
        if t.kind == "OP" and t.text == "(":
            depth += 1
        elif t.kind == "OP" and t.text == ")":
            depth -= 1
            i += 1
            if depth == 0:
                return i
        elif depth == 0 and t.kind == "OP" and t.text == ",":
            return i
        i += 1
    return i


# ---------------------------------------------------------------------------
# 参数声明与绑定值校验
# ---------------------------------------------------------------------------

def validate_parameters(parameter_types: list[str],
                        parameters: list | None) -> None:
    for idx, t in enumerate(parameter_types, start=1):
        if t not in ALL_TYPES:
            raise CompileError(
                "invalid_parameter",
                f"参数 ${idx} 声明类型 {t!r} 非法，仅 integer/text/boolean/null")
    if parameters is None:
        return
    if len(parameters) != len(parameter_types):
        raise CompileError(
            "invalid_parameter",
            f"绑定值 {len(parameters)} 个与声明 {len(parameter_types)} 个不一致")
    for idx, (t, v) in enumerate(zip(parameter_types, parameters), start=1):
        if v is None:
            continue  # 任意声明类型都允许绑定 NULL
        if t == INTEGER and (not isinstance(v, int) or isinstance(v, bool)):
            raise CompileError(
                "invalid_parameter",
                f"参数 ${idx} 声明 integer，实际绑定 {type(v).__name__}")
        if t == TEXT and not isinstance(v, str):
            raise CompileError(
                "invalid_parameter",
                f"参数 ${idx} 声明 text，实际绑定 {type(v).__name__}")
        if t == BOOLEAN and not isinstance(v, bool):
            raise CompileError(
                "invalid_parameter",
                f"参数 ${idx} 声明 boolean，实际绑定 {type(v).__name__}")
        if t == NULLTYPE and v is not None:
            raise CompileError(
                "invalid_parameter",
                f"参数 ${idx} 声明 null，只能绑定 NULL")


# ---------------------------------------------------------------------------
# 类型推断环境
# ---------------------------------------------------------------------------

class Scope:
    """单个 SELECT 层级可见的列环境。"""

    def __init__(self, relation: str, schema: TableSchema,
                 cte_outputs: Optional[dict[str, list[tuple[str, str]]]] = None):
        self.relation = relation
        self.schema = schema
        self.cte_outputs = cte_outputs or {}  # cte 名 -> [(输出列名, 逻辑类型)]

    def resolve(self, ref: ast.ColumnRef) -> str:
        if ref.table is not None:
            qualifier = ref.table.strip('"')
            if qualifier != self.relation:
                if qualifier in self.cte_outputs:
                    raise CompileError(
                        "scope_violation",
                        f"限定名 {qualifier}.{ref.bare_name} 与当前关系 "
                        f"{self.relation} 不一致", ref.src_pos)
                raise CompileError(
                    "scope_violation",
                    f"未知关系限定名 {qualifier}", ref.src_pos)
            return self.schema.type_of(ref.bare_name, ref.src_pos)
        if self.relation in self.cte_outputs:
            names = [n for n, _ in self.cte_outputs[self.relation]]
            if ref.bare_name not in names:
                raise CompileError(
                    "parse_error",
                    f"CTE {self.relation} 不存在输出列 {ref.bare_name}",
                    ref.src_pos)
            return dict(self.cte_outputs[self.relation])[ref.bare_name]
        return self.schema.type_of(ref.bare_name, ref.src_pos)


class TypeChecker:
    """对整棵 SELECT（含一个 CTE）做类型推断，产出每表达式逻辑类型。"""

    def __init__(self, tables: dict[str, TableSchema],
                 parameter_types: list[str]):
        self.tables = tables
        self.param_types = list(parameter_types)
        self.node_types: dict[int, str] = {}
        self.cte_outputs: dict[str, list[tuple[str, str]]] = {}

    def infer(self, sel: ast.Select) -> dict[int, str]:
        if sel.cte is not None:
            self._infer_select(sel.cte.query, sel.cte)
        self._infer_select(sel, None)
        return self.node_types

    def _scope_for(self, sel: ast.Select) -> Scope:
        if sel.table is None:
            raise CompileError("scope_violation",
                               "缺少 FROM 子句的报表不在 scope 内",
                               sel.src_pos)
        table = sel.table.strip('"')
        if table in self.cte_outputs:
            virtual = TableSchema(
                table,
                [n for n, _ in self.cte_outputs[table]],
                {n: t for n, t in self.cte_outputs[table]})
            return Scope(table, virtual, self.cte_outputs)
        if table not in self.tables:
            raise CompileError("scope_violation",
                               f"FROM 引用未知关系 {table}", sel.src_pos)
        return Scope(table, self.tables[table], self.cte_outputs)

    def _infer_select(self, sel: ast.Select, cte: Optional[ast.Cte]) -> None:
        scope = self._scope_for(sel)
        for item in sel.items:
            t = self._infer_expr(item.expr, scope)
            self.node_types[id(item)] = t
        if sel.where is not None:
            w = self._infer_expr(sel.where, scope)
            if not compatible(w, BOOLEAN):
                raise CompileError("type_error",
                                   "WHERE 条件必须是 boolean 表达式",
                                   sel.where.src_pos)
        for oi in sel.order_by:
            self._infer_expr(oi.expr, scope)
        for key in sel.distinct_on:
            self._infer_expr(key, scope)
        if cte is not None:
            outputs: list[tuple[str, str]] = []
            for idx, item in enumerate(sel.items):
                name = item.alias
                if name is None:
                    if isinstance(item.expr, ast.ColumnRef):
                        name = item.expr.bare_name
                    else:
                        raise CompileError(
                            "scope_violation",
                            "CTE 输出中的非列表达式必须显式声明别名，"
                            "以固定内外名称与输出列顺序", item.src_pos)
                outputs.append((name, self.node_types[id(item)]))
            if cte.columns is not None:
                if len(cte.columns) != len(outputs):
                    raise CompileError(
                        "scope_violation",
                        f"CTE {cte.name} 的列名清单 {len(cte.columns)} 列与"
                        f"内部 SELECT {len(outputs)} 列不一致", cte.src_pos)
                outputs = [(outer, t) for (outer, _), t in
                           zip(cte.columns, [t for _, t in outputs])]
            self.cte_outputs[cte.name] = outputs

    # ---- 表达式推断 ------------------------------------------------------
    def _infer_expr(self, node: ast.Expr, scope: Scope) -> str:
        m = getattr(self, "_infer_" + type(node).__name__, None)
        if m is None:  # pragma: no cover - 穷尽 AST 后不可达
            raise CompileError("unsupported_syntax",
                               f"未实现类型推断的节点 {type(node).__name__}",
                               node.src_pos)
        t = m(node, scope)
        self.node_types[id(node)] = t
        return t

    def _infer_ColumnRef(self, n: ast.ColumnRef, scope: Scope) -> str:
        return scope.resolve(n)

    def _infer_IntegerLit(self, n, scope) -> str:
        return INTEGER

    def _infer_StringLit(self, n, scope) -> str:
        return TEXT

    def _infer_NullLit(self, n, scope) -> str:
        return NULLTYPE

    def _infer_BoolLit(self, n, scope) -> str:
        return BOOLEAN

    def _infer_Param(self, n: ast.Param, scope: Scope) -> str:
        if n.index < 1 or n.index > len(self.param_types):
            raise CompileError(
                "invalid_parameter",
                f"${n.index} 超出 parameter_types 声明范围"
                f"（共 {len(self.param_types)} 个）", n.src_pos)
        return self.param_types[n.index - 1]

    def _infer_UnaryOp(self, n: ast.UnaryOp, scope: Scope) -> str:
        inner = self._infer_expr(n.operand, scope)
        if n.op == "NOT":
            if not compatible(inner, BOOLEAN):
                raise CompileError("type_error",
                                   "NOT 仅适用于 boolean 表达式", n.src_pos)
            return BOOLEAN
        if not compatible(inner, INTEGER):
            raise CompileError("type_error",
                               "一元负号仅适用于 integer 表达式", n.src_pos)
        return INTEGER

    def _infer_BinaryOp(self, n: ast.BinaryOp, scope: Scope) -> str:
        lt = self._infer_expr(n.left, scope)
        rt = self._infer_expr(n.right, scope)
        if n.op in ("AND", "OR"):
            if not compatible(lt, BOOLEAN) or not compatible(rt, BOOLEAN):
                raise CompileError("type_error",
                                   f"{n.op} 两侧必须是 boolean", n.src_pos)
            return BOOLEAN
        if n.op in COMPARISON_OPS:
            if not compatible(lt, rt):
                raise CompileError(
                    "type_error",
                    f"比较 {n.op} 两侧类型不兼容：{lt} 与 {rt}（不做隐式转换）",
                    n.src_pos)
            return BOOLEAN
        if n.op in ARITH_OPS:
            if not compatible(lt, INTEGER) or not compatible(rt, INTEGER):
                raise CompileError(
                    "type_error",
                    f"算术 {n.op} 仅支持 integer，得到 {lt} 与 {rt}",
                    n.src_pos)
            return INTEGER
        raise CompileError("unsupported_syntax",
                           f"二元操作符 {n.op} 不在 scope 内", n.src_pos)  # pragma

    def _infer_IsDistinctFrom(self, n: ast.IsDistinctFrom, scope: Scope) -> str:
        lt = self._infer_expr(n.left, scope)
        rt = self._infer_expr(n.right, scope)
        # 两个 NULL / 一个 NULL / 布尔比较都合法；但非空两侧仍需同类型
        if not compatible(lt, rt):
            raise CompileError(
                "type_error",
                f"IS DISTINCT FROM 两侧类型不兼容：{lt} 与 {rt}", n.src_pos)
        return BOOLEAN

    def _infer_IsNull(self, n: ast.IsNull, scope: Scope) -> str:
        self._infer_expr(n.operand, scope)
        return BOOLEAN

    def _infer_InList(self, n: ast.InList, scope: Scope) -> str:
        lt = self._infer_expr(n.operand, scope)
        for item in n.items:
            it = self._infer_expr(item, scope)
            if not compatible(lt, it):
                raise CompileError("type_error",
                                   "IN 列表元素与左值类型不兼容", n.src_pos)
        return BOOLEAN

    def _infer_Between(self, n: ast.Between, scope: Scope) -> str:
        vt = self._infer_expr(n.operand, scope)
        lo = self._infer_expr(n.low, scope)
        hi = self._infer_expr(n.high, scope)
        if not compatible(vt, lo) or not compatible(vt, hi):
            raise CompileError("type_error",
                               "BETWEEN 边界与值类型不兼容", n.src_pos)
        return BOOLEAN

    def _infer_Like(self, n: ast.Like, scope: Scope) -> str:
        vt = self._infer_expr(n.operand, scope)
        pt = self._infer_expr(n.pattern, scope)
        if not compatible(vt, TEXT) or not compatible(pt, TEXT):
            raise CompileError("type_error",
                               "LIKE 仅适用于 text 表达式", n.src_pos)
        return BOOLEAN

    def _infer_FuncCall(self, n: ast.FuncCall, scope: Scope) -> str:
        if n.distinct:
            raise CompileError("unsupported_syntax",
                               "聚合式 DISTINCT 函数参数不在 scope 内",
                               n.src_pos)
        if n.name == "CONCAT":
            if not n.args:
                raise CompileError("type_error",
                                   "concat() 至少需要一个参数", n.src_pos)
            for a in n.args:
                at = self._infer_expr(a, scope)
                if not compatible(at, TEXT):
                    raise CompileError(
                        "type_error",
                        f"concat() 只接受声明为 text 的参数，遇到 {at}；"
                        "NULL 参数按空串处理但非文本类型拒绝", a.src_pos)
            return TEXT
        raise CompileError("unsupported_syntax",
                           f"函数 {n.name} 不在 scope 内", n.src_pos)  # pragma

    def _infer_CastExpr(self, n: ast.CastExpr, scope: Scope) -> str:
        inner = self._infer_expr(n.operand, scope)
        target = {
            "INTEGER": INTEGER, "INT": INTEGER,
            "TEXT": TEXT, "BOOLEAN": BOOLEAN, "BOOL": BOOLEAN,
        }[n.type_name]
        if not compatible(inner, target):
            raise CompileError(
                "type_error",
                f"CAST({inner} AS {n.type_name}) 类型不兼容且不做隐式转换",
                n.src_pos)
        return target

    def _infer_CaseExpr(self, n: ast.CaseExpr, scope: Scope) -> str:
        result_types: list[str] = []
        if n.operand is not None:
            ot = self._infer_expr(n.operand, scope)
        else:
            ot = None
        for cond, res in n.whens:
            ct = self._infer_expr(cond, scope)
            if ot is None:
                if not compatible(ct, BOOLEAN):
                    raise CompileError("type_error",
                                       "CASE WHEN 条件必须是 boolean",
                                       n.src_pos)
            elif not compatible(ct, ot):
                raise CompileError("type_error",
                                   "CASE 简单形式的 WHEN 值与判别式类型不一致",
                                   n.src_pos)
            result_types.append(self._infer_expr(res, scope))
        if n.default is not None:
            result_types.append(self._infer_expr(n.default, scope))
        out = NULLTYPE
        for t in result_types:
            if not compatible(out, t):
                raise CompileError("type_error",
                                   "CASE 各分支结果类型不一致", n.src_pos)
            out = combine(out, t)
        return out


COMPARISON_OPS = frozenset({"=", "<>", "!=", "<", ">", "<=", ">="})
ARITH_OPS = frozenset({"+", "-", "*", "/", "%"})
