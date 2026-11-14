"""跨装配约束: 引用两个明确发生路径的有效选项, 必须在同一份配置实例的实际
发生树上检查 —— 不是各子树独立验证通过就算整体可制造。

引用路径未发生或该处取值未知 → 约束未决(pending), 并给出未决依据
(哪一侧、哪条边、是条件不成立还是条件未确定)。violated 与 pending 都阻断发布。
"""
from __future__ import annotations

from typing import Any

from .diag import Diag
from .expr import UNKNOWN, ExprError, evaluate, json_eq
from .expand import OccNode
from .model import ConstraintSide, Model


def evaluate_constraints(model: Model, root: OccNode, diags: list[Diag]) -> list[dict]:
    index: dict[tuple[str, ...], OccNode] = {}

    def walk(n: OccNode) -> None:
        index[n.path] = n
        for c in n.children:
            walk(c)

    walk(root)
    results: list[dict] = []
    for c in model.constraints:
        left = _resolve(model, index, c.left)
        right = _resolve(model, index, c.right)
        if left["status"] == "ok" and right["status"] == "ok":
            if c.op == "equal_options":
                good = json_eq(left["value"], right["value"])
            else:  # not_both_true
                good = not (left["value"] is True and right["value"] is True)
            status = "satisfied" if good else "violated"
        else:
            status = "pending"
        basis = {"left": left, "right": right}
        results.append({"id": c.id, "op": c.op, "status": status, "basis": basis})
        if status == "violated":
            diags.append(Diag("CONSTRAINT_VIOLATED", "error",
                              f"跨装配约束 {c.id}({c.op}) 在同一配置实例上被违反",
                              None, basis))
        elif status == "pending":
            diags.append(Diag("CONSTRAINT_PENDING", "error",
                              f"跨装配约束 {c.id}({c.op}) 未决: 引用路径未发生或取值未知",
                              None, basis))
    results.sort(key=lambda r: r["id"])
    return results


def _resolve(model: Model, index: dict, side: ConstraintSide) -> dict:
    """解析约束一侧: 返回 ok(含取值) / unknown(取值未知) / absent(路径未发生)。"""
    out: dict[str, Any] = {"path": side.path, "option": side.option}
    segments = tuple(side.path.split("."))
    node = index.get(segments)
    if node is None:
        out["status"] = "absent"
        out["detail"] = _absent_reason(model, index, segments)
        return out
    value = node.scope.get(side.option, UNKNOWN)
    if value is UNKNOWN:
        out["status"] = "unknown"
        out["detail"] = {"reason": "option_unbound",
                         "message": f"选项 {side.option!r} 在该发生上未赋值"}
        return out
    out["status"] = "ok"
    out["value"] = value
    return out


def _absent_reason(model: Model, index: dict, segments: tuple[str, ...]) -> dict:
    """路径未发生时, 沿最深已发生前缀定位原因: 条件不成立 / 条件未确定 / 无此子项。"""
    for i in range(len(segments) - 1, 0, -1):
        node = index.get(segments[:i])
        if node is None:
            continue
        nxt = segments[i]
        at = ".".join(segments[:i])
        defn = model.definitions.get(node.ref)
        if defn is None or defn.kind != "assembly":
            return {"reason": "parent_not_assembly", "at": at}
        child = next((ch for ch in defn.children if ch.id == nxt), None)
        if child is None:
            return {"reason": "no_such_child", "at": at, "missing": nxt}
        try:
            val = evaluate(child.when, node.scope) if child.when is not None else True
        except ExprError:
            return {"reason": "condition_error", "at": at, "child": nxt}
        if val is UNKNOWN:
            return {"reason": "condition_unknown", "at": at, "child": nxt,
                    "message": "该边启用条件未确定(见 GAP_UNKNOWN_CONDITION)"}
        if val is False:
            return {"reason": "condition_false", "at": at, "child": nxt,
                    "message": "该边启用条件在本配置下不成立"}
        return {"reason": "not_expanded", "at": at, "child": nxt,
                "message": "该边未实际展开(见其他诊断, 如激活环)"}
    return {"reason": "root_mismatch"}
