"""输入模型的加载与静态校验: 装配定义、选项声明、跨装配约束、销售配置。

所有校验问题都收集为诊断(尽量继续展开, 一次性报告全部问题), 只有文件不可读 /
JSON 非法等致命问题才抛异常。JSON 解析带重复键检测, 避免重复定义被静默覆盖。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .diag import Diag
from .expr import ExprError, referenced_options

CONSTRAINT_OPS = ("equal_options", "not_both_true")


@dataclass(frozen=True)
class Child:
    id: str                       # 发生ID, 同一父定义内唯一
    ref: str                      # 目标定义
    qty: int                      # 正整数
    when: Any                     # 启用条件(JSON 表达式)或 None(恒 true)
    overrides: dict[str, Any]     # 局部选项覆盖(常量), 作用于该次发生及后代


@dataclass
class Definition:
    id: str
    kind: str                     # "assembly" | "part"
    unit: str | None              # part 的统一计量单位
    children: list[Child]


@dataclass(frozen=True)
class ConstraintSide:
    path: str                     # 点分发生路径, 如 root.left.drive
    option: str


@dataclass(frozen=True)
class Constraint:
    id: str
    op: str                       # equal_options | not_both_true
    left: ConstraintSide
    right: ConstraintSide


@dataclass
class Model:
    definitions: dict[str, Definition]
    options: dict[str, tuple]     # 选项名 -> 允许取值
    constraints: list[Constraint]


@dataclass
class Config:
    name: str
    root_definition: str
    root_id: str
    options: dict[str, Any]                                # 根作用域(已校验)
    instance_overrides: dict[tuple[str, ...], dict[str, Any]]  # 精确路径 -> 覆盖


def load_json(path: str | Path) -> Any:
    """读取 JSON 并检测重复键(重复定义/重复发生ID会被静默吞掉, 必须报错)。"""
    path = Path(path)

    def hook(pairs):
        obj = {}
        for k, v in pairs:
            if k in obj:
                raise ValueError(f"{path}: 重复的 JSON 键 {k!r}")
            obj[k] = v
        return obj

    with open(path, encoding="utf-8") as f:
        return json.load(f, object_pairs_hook=hook)


def domain_contains(allowed: tuple, v: Any) -> bool:
    return any(type(a) is type(v) and a == v for a in allowed)


def _is_const(v: Any) -> bool:
    return v is None or isinstance(v, (bool, int, float, str))


def _check_overrides(ovr: Any, options: dict[str, tuple], diags: list[Diag],
                     where: str, code_prefix: str) -> dict[str, Any]:
    """校验一组选项覆盖: 必须是常量、选项已声明、取值在声明域内。"""
    out: dict[str, Any] = {}
    if ovr is None:
        return out
    if not isinstance(ovr, dict):
        diags.append(Diag(f"{code_prefix}_BAD", "error", f"{where}: 覆盖必须是对象"))
        return out
    for name, value in ovr.items():
        if name not in options:
            diags.append(Diag(f"{code_prefix}_UNDECLARED_OPTION", "error",
                              f"{where}: 覆盖了未声明的选项 {name!r}", basis={"option": name}))
            continue
        if not _is_const(value):
            diags.append(Diag(f"{code_prefix}_NOT_CONSTANT", "error",
                              f"{where}: 选项覆盖只支持常量, 不支持表达式",
                              basis={"option": name}))
            continue
        if not domain_contains(options[name], value):
            diags.append(Diag(f"{code_prefix}_VALUE_DOMAIN", "error",
                              f"{where}: 选项 {name!r} 的取值 {value!r} 不在声明域 {list(options[name])} 内",
                              basis={"option": name, "value": value}))
            continue
        out[name] = value
    return out


def _check_when(when: Any, options: dict[str, tuple], diags: list[Diag], where: str) -> None:
    try:
        refs = referenced_options(when)
    except ExprError as e:
        diags.append(Diag("EXPR_ERROR", "error", f"{where}: 条件表达式非法: {e}"))
        return
    for name in sorted(refs):
        if name not in options:
            diags.append(Diag("EXPR_UNDECLARED_OPTION", "error",
                              f"{where}: 条件引用了未声明的选项 {name!r}",
                              basis={"option": name}))


def load_model_data(defs_data: Any, opts_data: Any, cons_data: Any) -> tuple[Model, list[Diag]]:
    diags: list[Diag] = []
    options: dict[str, tuple] = {}
    definitions: dict[str, Definition] = {}
    constraints: list[Constraint] = []

    # ---- 选项声明 ----
    if not isinstance(opts_data, dict):
        raise ValueError("options 必须是对象: {选项名: [允许取值, ...]}")
    for name, allowed in opts_data.items():
        if not isinstance(allowed, list) or not allowed or not all(_is_const(v) for v in allowed):
            diags.append(Diag("OPT_BAD_DECLARATION", "error",
                              f"选项 {name!r} 的声明域必须是非空常量数组"))
            continue
        options[name] = tuple(allowed)

    # ---- 装配/零件定义 ----
    if not isinstance(defs_data, dict):
        raise ValueError("definitions 必须是对象: {定义id: 定义}")
    for def_id, body in defs_data.items():
        where = f"定义 {def_id!r}"
        if not isinstance(body, dict):
            diags.append(Diag("DEF_BAD_DEFINITION", "error", f"{where}: 必须是对象"))
            continue
        kind = body.get("kind")
        if kind == "part":
            unit = body.get("unit")
            if not isinstance(unit, str) or not unit:
                diags.append(Diag("DEF_BAD_DEFINITION", "error", f"{where}: 零件缺少计量单位 unit"))
                continue
            definitions[def_id] = Definition(def_id, "part", unit, [])
            continue
        if kind != "assembly":
            diags.append(Diag("DEF_BAD_DEFINITION", "error",
                              f"{where}: kind 必须是 assembly 或 part"))
            continue
        raw_children = body.get("children", [])
        if not isinstance(raw_children, list):
            diags.append(Diag("DEF_BAD_DEFINITION", "error", f"{where}: children 必须是数组"))
            continue
        children: list[Child] = []
        seen: set[str] = set()
        for i, raw in enumerate(raw_children):
            cwhere = f"{where} 子项[{i}]"
            if not isinstance(raw, dict):
                diags.append(Diag("DEF_BAD_CHILD", "error", f"{cwhere}: 必须是对象"))
                continue
            cid, ref = raw.get("id"), raw.get("ref")
            ok = True
            if not isinstance(cid, str) or not cid or "." in cid:
                diags.append(Diag("DEF_BAD_CHILD", "error",
                                  f"{cwhere}: 发生ID必须是非空且不含 '.' 的字符串"))
                ok = False
            if not isinstance(ref, str) or not ref:
                diags.append(Diag("DEF_BAD_CHILD", "error", f"{cwhere}: 缺少目标定义 ref"))
                ok = False
            if not ok:
                continue
            qty = raw.get("qty", 1)
            if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
                diags.append(Diag("DEF_BAD_QTY", "error",
                                  f"{cwhere}({cid}): qty 必须是正整数, 得到 {qty!r}"))
                continue
            when = raw.get("when")
            if when is not None:
                _check_when(when, options, diags, f"{cwhere}({cid})")
            overrides = _check_overrides(raw.get("overrides"), options, diags,
                                         f"{cwhere}({cid})", "OVERRIDE")
            if cid in seen:
                diags.append(Diag("DEF_DUPLICATE_CHILD", "error",
                                  f"{where}: 发生ID {cid!r} 在同一父定义内重复, 仅保留第一次出现",
                                  basis={"child": cid}))
                continue
            seen.add(cid)
            children.append(Child(cid, ref, qty, when, overrides))
        definitions[def_id] = Definition(def_id, "assembly", None, children)

    # 目标引用存在性(在全部定义收集完之后检查)
    for defn in definitions.values():
        for child in defn.children:
            if child.ref not in definitions:
                diags.append(Diag("DEF_UNKNOWN_REF", "error",
                                  f"定义 {defn.id!r} 的子项 {child.id!r} 引用了未定义的 {child.ref!r}",
                                  basis={"child": child.id, "ref": child.ref}))

    # ---- 跨装配约束 ----
    if not isinstance(cons_data, list):
        raise ValueError("constraints 必须是数组")
    for i, raw in enumerate(cons_data):
        cwhere = f"约束[{i}]"
        if not isinstance(raw, dict):
            diags.append(Diag("CONSTRAINT_BAD_DEFINITION", "error", f"{cwhere}: 必须是对象"))
            continue
        cid, op = raw.get("id"), raw.get("op")
        sides = []
        ok = isinstance(cid, str) and bool(cid)
        if not ok:
            diags.append(Diag("CONSTRAINT_BAD_DEFINITION", "error", f"{cwhere}: 缺少 id"))
        if op not in CONSTRAINT_OPS:
            diags.append(Diag("CONSTRAINT_BAD_DEFINITION", "error",
                              f"{cwhere}({cid}): op 必须是 {list(CONSTRAINT_OPS)}"))
            ok = False
        for side_name in ("left", "right"):
            side = raw.get(side_name)
            if not isinstance(side, dict) or not isinstance(side.get("path"), str) \
                    or not isinstance(side.get("option"), str):
                diags.append(Diag("CONSTRAINT_BAD_DEFINITION", "error",
                                  f"{cwhere}({cid}): {side_name} 需要 {{path, option}}"))
                ok = False
                continue
            if not all(side["path"].split(".")):
                diags.append(Diag("CONSTRAINT_BAD_DEFINITION", "error",
                                  f"{cwhere}({cid}): {side_name}.path 含空段"))
                ok = False
                continue
            if side["option"] not in options:
                diags.append(Diag("CONSTRAINT_UNDECLARED_OPTION", "error",
                                  f"{cwhere}({cid}): 引用了未声明的选项 {side['option']!r}",
                                  basis={"option": side["option"]}))
                ok = False
                continue
            sides.append(ConstraintSide(side["path"], side["option"]))
        if ok and len(sides) == 2:
            constraints.append(Constraint(cid, op, sides[0], sides[1]))

    return Model(definitions, options, constraints), diags


def load_model(defs_path: str | Path, opts_path: str | Path,
               cons_path: str | Path) -> tuple[Model, list[Diag]]:
    return load_model_data(load_json(defs_path), load_json(opts_path), load_json(cons_path))


def load_config_data(model: Model, data: Any, name: str) -> tuple[Config, list[Diag]]:
    diags: list[Diag] = []
    if not isinstance(data, dict):
        raise ValueError("配置必须是对象")
    root_def = data.get("root_definition")
    root_id = data.get("root_id")
    if not isinstance(root_def, str) or root_def not in model.definitions \
            or model.definitions[root_def].kind != "assembly":
        diags.append(Diag("CONFIG_BAD_ROOT", "error",
                          f"root_definition {root_def!r} 不存在或不是装配定义"))
    if not isinstance(root_id, str) or not root_id or "." in root_id:
        diags.append(Diag("CONFIG_BAD_ROOT", "error",
                          f"root_id 必须是非空且不含 '.' 的字符串, 得到 {root_id!r}"))
        root_id = "root"

    options: dict[str, Any] = {}
    raw_opts = data.get("options", {})
    if not isinstance(raw_opts, dict):
        raise ValueError("配置 options 必须是对象")
    for opt, value in raw_opts.items():
        if opt not in model.options:
            diags.append(Diag("CONFIG_UNDECLARED_OPTION", "error",
                              f"配置给未声明的选项 {opt!r} 赋值", basis={"option": opt}))
            continue
        if not domain_contains(model.options[opt], value):
            diags.append(Diag("CONFIG_VALUE_DOMAIN", "error",
                              f"选项 {opt!r} 的取值 {value!r} 不在声明域 {list(model.options[opt])} 内",
                              basis={"option": opt, "value": value}))
            continue
        options[opt] = value

    instance_overrides: dict[tuple[str, ...], dict[str, Any]] = {}
    raw_iov = data.get("instance_overrides", {})
    if not isinstance(raw_iov, dict):
        raise ValueError("配置 instance_overrides 必须是对象")
    for path_str, ovr in raw_iov.items():
        segments = tuple(path_str.split("."))
        if not all(segments) or segments[0] != root_id:
            diags.append(Diag("INSTANCE_OVERRIDE_BAD_PATH", "error",
                              f"实例覆盖路径 {path_str!r} 必须以根发生 {root_id!r} 开头且不含空段",
                              basis={"path": path_str}))
            continue
        checked = _check_overrides(ovr, model.options, diags,
                                   f"实例覆盖 {path_str!r}", "INSTANCE_OVERRIDE")
        instance_overrides[segments] = checked

    return Config(name, root_def, root_id, options, instance_overrides), diags


def load_config(model: Model, path: str | Path) -> tuple[Config, list[Diag]]:
    path = Path(path)
    return load_config_data(model, load_json(path), name=path.stem)
