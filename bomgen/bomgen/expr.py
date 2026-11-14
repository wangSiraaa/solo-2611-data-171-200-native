"""材料规定的小型 JSON 条件表达式: eq / all / any / not / option, 三值强 Kleene 逻辑。

条件是纯 JSON 数据而非代码, 不执行任意代码。求值只是在有限表达式树上的结构
递归: option 只读取当前发生的有效选项(销售配置/边覆盖/实例覆盖给出的常量),
不会触发其他条件的求值, 因此条件不可能自引用, 求值必然终止(另加深度上限兜底)。
缺失选项取 UNKNOWN, 绝不默认成 false —— 否则可能需要的物料会被悄悄藏掉。
"""
from __future__ import annotations

from typing import Any, Mapping


class _Unknown:
    """三值逻辑中的"未知"单例。"""
    __slots__ = ()

    def __repr__(self) -> str:
        return "UNKNOWN"

    def __bool__(self) -> bool:
        raise TypeError("UNKNOWN 不能当作布尔值使用")


UNKNOWN = _Unknown()

MAX_EXPR_DEPTH = 64


class ExprError(Exception):
    """条件表达式的结构或类型错误(属于模型缺陷, 不是运行时配置问题)。"""


def _is_scalar(v: Any) -> bool:
    return v is None or isinstance(v, (bool, int, float, str))


def json_eq(a: Any, b: Any) -> bool:
    """严格相等, 避免 Python 里 True == 1 之类的隐式等价。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    return a == b


def _need_bool(v: Any, op: str) -> bool:
    if isinstance(v, bool):
        return v
    raise ExprError(f"运算符 {op} 的操作数必须是布尔值, 得到 {v!r}")


def evaluate(expr: Any, scope: Mapping[str, Any], _depth: int = 0) -> Any:
    """在有效选项作用域 scope 上求值, 返回 True/False/UNKNOWN 或字面值。"""
    if _depth > MAX_EXPR_DEPTH:
        raise ExprError(f"表达式嵌套超过 {MAX_EXPR_DEPTH} 层")
    if isinstance(expr, dict):
        if len(expr) != 1:
            raise ExprError(f"表达式对象必须恰好含一个运算符: {sorted(expr)}")
        (op, arg), = expr.items()
        if op == "option":
            if not isinstance(arg, str):
                raise ExprError("option 需要选项名字符串")
            return scope.get(arg, UNKNOWN)
        if op == "eq":
            a, b = _pair(arg, op)
            va = evaluate(a, scope, _depth + 1)
            vb = evaluate(b, scope, _depth + 1)
            if va is UNKNOWN or vb is UNKNOWN:
                return UNKNOWN
            return json_eq(va, vb)
        if op == "not":
            v = evaluate(arg, scope, _depth + 1)
            if v is UNKNOWN:
                return UNKNOWN
            return not _need_bool(v, op)
        if op in ("all", "any"):
            items = _items(arg, op)
            saw_unknown = False
            for sub in items:
                v = evaluate(sub, scope, _depth + 1)
                if v is UNKNOWN:
                    saw_unknown = True
                    continue
                b = _need_bool(v, op)
                if op == "all" and not b:
                    return False
                if op == "any" and b:
                    return True
            if saw_unknown:
                return UNKNOWN
            return op == "all"
        raise ExprError(f"未知运算符 {op!r}")
    if _is_scalar(expr):
        return expr
    raise ExprError(f"非法表达式: {expr!r}")


def referenced_options(expr: Any, _depth: int = 0) -> set[str]:
    """静态收集表达式引用的选项名, 同时校验结构合法性。"""
    if _depth > MAX_EXPR_DEPTH:
        raise ExprError(f"表达式嵌套超过 {MAX_EXPR_DEPTH} 层")
    if isinstance(expr, dict):
        if len(expr) != 1:
            raise ExprError(f"表达式对象必须恰好含一个运算符: {sorted(expr)}")
        (op, arg), = expr.items()
        if op == "option":
            if not isinstance(arg, str):
                raise ExprError("option 需要选项名字符串")
            return {arg}
        if op == "eq":
            a, b = _pair(arg, op)
            return referenced_options(a, _depth + 1) | referenced_options(b, _depth + 1)
        if op == "not":
            return referenced_options(arg, _depth + 1)
        if op in ("all", "any"):
            out: set[str] = set()
            for sub in _items(arg, op):
                out |= referenced_options(sub, _depth + 1)
            return out
        raise ExprError(f"未知运算符 {op!r}")
    if _is_scalar(expr):
        return set()
    raise ExprError(f"非法表达式: {expr!r}")


def _pair(arg: Any, op: str) -> tuple[Any, Any]:
    if not isinstance(arg, list) or len(arg) != 2:
        raise ExprError(f"{op} 需要二元数组")
    return arg[0], arg[1]


def _items(arg: Any, op: str) -> list:
    if not isinstance(arg, list) or not arg:
        raise ExprError(f"{op} 需要非空数组")
    return arg
