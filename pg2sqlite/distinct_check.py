"""独立于两个数据库包装器的 DISTINCT ON 选行核对。

直接按 PostgreSQL 语义在 Python 里重放：
WHERE 过滤 -> 按 ORDER BY 全键排序 -> 每个分组键取首行 -> 按完整 ORDER BY
展示。若源 ORDER BY 经编译校验已含 event_id，则排序为全序，结果唯一确定。
本模块不执行 SQL，仅消费 rows.json 的同一批小数据。
"""

from __future__ import annotations

from . import ast_nodes as ast
from .errors import CompileError


class RowEnv:
    def __init__(self, row: dict):
        self.row = row

    def get(self, ref: ast.ColumnRef):
        name = ref.bare_name
        if name not in self.row:
            raise CompileError("parse_error",
                               f"手工核对数据缺少列 {name}", ref.src_pos)
        return self.row[name]


def _eval(node: ast.Expr, env: RowEnv, params: list):
    if isinstance(node, ast.ColumnRef):
        return env.get(node)
    if isinstance(node, ast.IntegerLit):
        return node.value
    if isinstance(node, ast.StringLit):
        return _unquote(node.text)
    if isinstance(node, ast.NullLit):
        return None
    if isinstance(node, ast.BoolLit):
        return node.value
    if isinstance(node, ast.Param):
        return params[node.index - 1]
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, env, params)
        if v is None:
            return None
        if node.op == "NOT":
            return not v
        return -v
    if isinstance(node, ast.BinaryOp):
        return _eval_binary(node, env, params)
    if isinstance(node, ast.IsNull):
        v = _eval(node.operand, env, params)
        return (v is not None) if node.negated else (v is None)
    if isinstance(node, ast.IsDistinctFrom):
        l = _eval(node.left, env, params)
        r = _eval(node.right, env, params)
        distinct = _is_distinct(l, r)
        return (not distinct) if node.negated else distinct
    if isinstance(node, ast.InList):
        v = _eval(node.operand, env, params)
        vals = [_eval(i, env, params) for i in node.items]
        hit = any(not _is_distinct(v, x) for x in vals)
        return (not hit) if node.negated else hit
    if isinstance(node, ast.Between):
        v = _eval(node.operand, env, params)
        lo = _eval(node.low, env, params)
        hi = _eval(node.high, env, params)
        hit = (not _is_distinct(v, None) and False) or (
            v is not None and lo is not None and hi is not None
            and lo <= v <= hi)
        return (not hit) if node.negated else hit
    if isinstance(node, ast.Like):
        import fnmatch
        v = _eval(node.operand, env, params)
        p = _eval(node.pattern, env, params)
        if v is None or p is None:
            return None
        pattern = p.replace("%", "*").replace("_", "?")
        hit = fnmatch.fnmatchcase(v, pattern)
        return (not hit) if node.negated else hit
    if isinstance(node, ast.FuncCall):
        if node.name == "CONCAT":
            out = []
            for a in node.args:
                v = _eval(a, env, params)
                out.append("" if v is None else str(v))
            return "".join(out)
    if isinstance(node, ast.CastExpr):
        v = _eval(node.operand, env, params)
        if v is None:
            return None
        if node.type_name in ("INTEGER", "INT"):
            return int(v)
        if node.type_name == "TEXT":
            return str(v)
        return bool(v)
    if isinstance(node, ast.CaseExpr):
        if node.operand is not None:
            disc = _eval(node.operand, env, params)
            for cond, res in node.whens:
                if not _is_distinct(disc, _eval(cond, env, params)):
                    return _eval(res, env, params)
        else:
            for cond, res in node.whens:
                if _eval(cond, env, params):
                    return _eval(res, env, params)
        return _eval(node.default, env, params) if node.default else None
    raise CompileError("unsupported_syntax",
                       f"手工核对未覆盖的表达式 {type(node).__name__}",
                       node.src_pos)


def _eval_binary(n: ast.BinaryOp, env: RowEnv, params):
    if n.op == "AND":
        l = _eval(n.left, env, params)
        r = _eval(n.right, env, params)
        if l is False or r is False:
            return False
        if l is None or r is None:
            return None
        return bool(l and r)
    if n.op == "OR":
        l = _eval(n.left, env, params)
        r = _eval(n.right, env, params)
        if l is True or r is True:
            return True
        if l is None or r is None:
            return None
        return bool(l or r)
    l = _eval(n.left, env, params)
    r = _eval(n.right, env, params)
    if l is None or r is None:
        return None
    if n.op in ("=", "<>", "!=", "<", ">", "<=", ">="):
        op = "!=" if n.op == "<>" else n.op
        import operator
        return getattr(operator, {
            "=": "eq", "!=": "ne", "<": "lt", ">": "gt",
            "<=": "le", ">=": "ge"}[op])(l, r)
    if n.op == "+":
        return l + r
    if n.op == "-":
        return l - r
    if n.op == "*":
        return l * r
    if n.op == "/":
        # 与 PG integer 除法对齐：向零截断
        q = abs(l) // abs(r)
        return (q if (l < 0) == (r < 0) else -q)
    if n.op == "%":
        return l - (l // r) * r if (l * r) >= 0 else l - (
            int(l / r)) * r
    raise CompileError("unsupported_syntax",
                       f"手工核对未覆盖操作符 {n.op}", n.src_pos)


def _is_distinct(a, b) -> bool:
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    return a != b


def _unquote(text: str) -> str:
    inner = text[1:-1]
    return inner.replace("''", "'")


def _sort_key(row: dict, order_by: list[ast.OrderItem], params: list):
    """生成与 PostgreSQL ORDER BY ... [NULLS FIRST|LAST] 等价的 Python 排序键。

    每个排序项输出两个键：
      null_bucket —— NULL 应落的桶：FIRST 时 NULL=0/非NULL=1，
                     LAST 时 NULL=1/非NULL=0；
      value_key   —— 非 NULL 值本身，DESC 时包一层反转比较器；
                     NULL 行给固定占位，因其已被 null_bucket 隔离。
    """
    env = RowEnv(row)
    keys = []
    for oi in order_by:
        v = _eval(oi.expr, env, params)
        nulls = oi.nulls or ("FIRST" if oi.direction == "ASC" else "LAST")
        is_null = v is None
        if nulls == "FIRST":
            null_bucket = 0 if is_null else 1
        else:
            null_bucket = 1 if is_null else 0
        if is_null:
            value_key = _Reversed(0) if oi.direction == "DESC" else 0
        elif oi.direction == "DESC":
            value_key = _Reversed(v)
        else:
            value_key = v
        keys.append((null_bucket, value_key))
    return keys


class _Reversed:
    """反转自然序的比较包装器；只用于同桶非空值之间，故不接收 None。"""

    __slots__ = ("v",)

    def __init__(self, v):
        self.v = v

    def __lt__(self, other):
        return self.v > other.v

    def __eq__(self, other):
        return self.v == other.v


def distinct_on_reference(sel: ast.Select, rows: list[dict],
                           params: list) -> list[dict]:
    """按 PG 语义生成 DISTINCT ON 的预期结果（含最终展示排序）。"""
    filtered = []
    for raw in rows:
        env = RowEnv(raw)
        if sel.where is None or _eval(sel.where, env, params):
            filtered.append(dict(raw))

    ordered = sorted(filtered, key=lambda r: _sort_key(r, sel.order_by, params))

    seen: set = set()
    picked: list[dict] = []
    for row in ordered:
        env = RowEnv(row)
        key = tuple(_repr_key(_eval(k, env, params)) for k in sel.distinct_on)
        if key in seen:
            continue
        seen.add(key)
        picked.append(row)

    # 最终展示顺序：对选中行再次按完整 ORDER BY 排序
    picked.sort(key=lambda r: _sort_key(r, sel.order_by, params))

    # 投影：严格按 SELECT 列顺序，应用别名
    result = []
    for row in picked:
        env = RowEnv(row)
        out = {}
        for idx, item in enumerate(sel.items):
            name = item.alias or (
                item.expr.bare_name if isinstance(item.expr, ast.ColumnRef)
                else f"column_{idx + 1}")
            out[name] = _eval(item.expr, env, params)
        result.append(out)
    return result


def _repr_key(v):
    return ("__n__",) if v is None else ("__v__", v)
