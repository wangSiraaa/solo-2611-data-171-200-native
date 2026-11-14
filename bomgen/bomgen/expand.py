"""按有效配置展开实际发生树(核心引擎)。

循环状态
--------
当前活动祖先链: 从根到当前节点的 (定义id, 有效选项状态) 序列。按 semantics.md,
"相同定义 + 相同有效选项状态" 在链上重复即本次已激活路径构成环 —— 只阻断该边并
诊断, 不无限递归。该状态是按路径维护的(进入压栈、离开弹栈), 不是全局 visited:
同一定义在兄弟位置复用(不同路径)会照常完整展开。选项域有限, 故链上状态数有限,
递归必然终止; 条件不确定的边不实际递归, 只记缺口。

缓存键
------
(定义id, 有效选项状态的规范化形式)。绝不只按定义编号缓存 —— 同一定义在不同
有效选项下是不同实例, 按定义编号缓存会串配置。缓存的是"该 (定义, 选项状态) 下
的相对子树"(路径相对、数量按子树根 qty=1 归一、诊断路径相对)。两类情况不缓存:
(1) 子树内出现激活环诊断(环判定依赖祖先链, 不可复用); (2) 子树下方存在实例覆盖
路径(实例覆盖按精确路径生效, 换路径复用会串)。复用前还要求子树内部状态集与当前
祖先链无交, 否则现场展开, 保证环判定不被缓存绕过。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .diag import Diag
from .expr import UNKNOWN, ExprError, evaluate, referenced_options
from .model import Config, Model


def scope_key(scope: dict[str, Any]) -> tuple:
    """有效选项状态的规范化形式: 只含已绑定选项, 排序后得到可哈希键。"""
    return tuple(sorted(scope.items()))


@dataclass
class OccNode:
    id: str
    ref: str                       # 目标定义
    kind: str                      # "assembly" | "part"
    unit: str | None
    edge_qty: int                  # 该边数量
    qty: int                       # 沿路径相乘后的累计数量
    scope: dict[str, Any]          # 该发生的有效选项(已绑定部分)
    path: tuple[str, ...]
    edge_overrides: dict[str, Any] = field(default_factory=dict)
    inst_overrides: dict[str, Any] = field(default_factory=dict)
    children: list["OccNode"] = field(default_factory=list)


@dataclass
class _CacheEntry:
    children: list[OccNode]        # 模板: 路径相对、数量按子树根 qty=1 归一
    diags: list[Diag]              # 路径相对的诊断
    states: frozenset              # 子树内展开过的 (定义id, 选项状态) 集合(含子树根)


def _normalize(node: OccNode, base_path: tuple[str, ...], base_qty: int) -> OccNode:
    """把子树转成可缓存模板: 路径相对化、数量按子树根归一(整除, 数量均为整数)。"""
    return OccNode(
        id=node.id, ref=node.ref, kind=node.kind, unit=node.unit,
        edge_qty=node.edge_qty, qty=node.qty // base_qty,
        scope=dict(node.scope), path=node.path[len(base_path):],
        edge_overrides=dict(node.edge_overrides), inst_overrides=dict(node.inst_overrides),
        children=[_normalize(c, base_path, base_qty) for c in node.children],
    )


def _clone(tpl: OccNode, base_path: tuple[str, ...], base_qty: int) -> OccNode:
    """把模板实例化到指定路径前缀与累计数量下。"""
    return OccNode(
        id=tpl.id, ref=tpl.ref, kind=tpl.kind, unit=tpl.unit,
        edge_qty=tpl.edge_qty, qty=tpl.qty * base_qty,
        scope=dict(tpl.scope), path=base_path + tpl.path,
        edge_overrides=dict(tpl.edge_overrides), inst_overrides=dict(tpl.inst_overrides),
        children=[_clone(c, base_path, base_qty) for c in tpl.children],
    )


def _states_of(node: OccNode) -> set[tuple[str, tuple]]:
    out = set()
    if node.kind == "assembly":
        out.add((node.ref, scope_key(node.scope)))
    for c in node.children:
        out |= _states_of(c)
    return out


class Expander:
    def __init__(self, model: Model, config: Config):
        self.model = model
        self.config = config
        self.cache: dict[tuple[str, tuple], _CacheEntry] = {}
        self.applied_inst_paths: set[tuple[str, ...]] = set()

    # ------------------------------------------------------------------ API
    def expand(self) -> tuple[OccNode, list[Diag]]:
        diags: list[Diag] = []
        root_path = (self.config.root_id,)
        root_scope = dict(self.config.options)
        inst = self.config.instance_overrides.get(root_path, {})
        if inst:
            root_scope.update(inst)
            self.applied_inst_paths.add(root_path)
        root = OccNode(self.config.root_id, self.config.root_definition, "assembly",
                       None, 1, 1, root_scope, root_path, {}, dict(inst), [])
        chain = ((root.ref, scope_key(root_scope), root_path),)
        self._expand_children(root, chain, diags)
        for p in sorted(self.config.instance_overrides):
            if p not in self.applied_inst_paths:
                diags.append(Diag("INSTANCE_OVERRIDE_UNUSED", "warning",
                                  "实例覆盖路径在本次配置中未发生, 覆盖未生效", p,
                                  {"overrides": self.config.instance_overrides[p]}))
        return root, diags

    # ------------------------------------------------------------- 内部实现
    def _inst_below(self, path: tuple[str, ...]) -> bool:
        """是否存在严格位于 path 之下的实例覆盖路径(存在则该子树不可缓存/复用)。"""
        return any(len(p) > len(path) and p[:len(path)] == path
                   for p in self.config.instance_overrides)

    def _expand_children(self, node: OccNode, chain: tuple, diags: list[Diag]) -> None:
        defn = self.model.definitions[node.ref]
        for child in defn.children:
            cpath = node.path + (child.id,)
            # 1) 启用条件在父发生的有效作用域上求值。边 overrides 只作用于该次发生
            #    及后代, 不影响自己的 when —— 从语义上杜绝条件自引用。
            if child.when is None:
                val: Any = True
            else:
                try:
                    val = evaluate(child.when, node.scope)
                except ExprError as e:
                    diags.append(Diag("EXPR_ERROR", "error",
                                      f"启用条件求值失败: {e}", cpath,
                                      {"when": child.when}))
                    continue
            if val is UNKNOWN:
                # 缺口: 缺失选项不默认 false, 该边不展开, 记录受影响路径。
                diags.append(Diag(
                    "GAP_UNKNOWN_CONDITION", "error",
                    "启用条件未确定(unknown), 该边不展开并记缺口, 不能发布完整BOM",
                    cpath,
                    {"when": child.when,
                     "unknown_options": sorted(o for o in referenced_options(child.when)
                                               if o not in node.scope)}))
                continue
            if not isinstance(val, bool):
                diags.append(Diag("EXPR_ERROR", "error",
                                  f"启用条件结果不是布尔值: {val!r}", cpath,
                                  {"when": child.when}))
                continue
            if val is False:
                continue  # 已确定不启用: 直接跳过, 不是缺口

            # 2) 该次发生的有效选项: 父作用域 → 边 overrides → 该路径实例覆盖
            cscope = dict(node.scope)
            cscope.update(child.overrides)
            inst = self.config.instance_overrides.get(cpath)
            if inst:
                cscope.update(inst)
                self.applied_inst_paths.add(cpath)
            cqty = node.qty * child.qty
            cdef = self.model.definitions.get(child.ref)
            if cdef is None:
                diags.append(Diag("DEF_UNKNOWN_REF", "error",
                                  f"引用了未定义的 {child.ref!r}", cpath,
                                  {"ref": child.ref}))
                continue
            if cdef.kind == "part":
                node.children.append(OccNode(child.id, child.ref, "part", cdef.unit,
                                             child.qty, cqty, cscope, cpath,
                                             dict(child.overrides), dict(inst or {}), []))
                continue

            # 3) 环判定: (定义, 有效选项状态) 已在活动祖先链上 → 本次激活的环
            state = (child.ref, scope_key(cscope))
            hit = next((i for i, (d, s, _) in enumerate(chain) if (d, s) == state), None)
            if hit is not None:
                loop = [{"definition": d, "path": ".".join(p)} for d, _, p in chain[hit:]]
                loop.append({"definition": child.ref, "path": ".".join(cpath)})
                diags.append(Diag(
                    "CYCLE_ACTIVE", "error",
                    "本次配置激活了定义环(相同定义+相同有效选项状态在活动祖先链上重复)",
                    cpath, {"cycle": loop}))
                continue
            cnode = OccNode(child.id, child.ref, "assembly", None, child.qty, cqty,
                            cscope, cpath, dict(child.overrides), dict(inst or {}), [])
            node.children.append(cnode)
            self._expand_assembly(cnode, chain, diags)

    def _expand_assembly(self, node: OccNode, chain: tuple, diags: list[Diag]) -> None:
        state = (node.ref, scope_key(node.scope))
        new_chain = chain + ((node.ref, state[1], node.path),)
        bypass = self._inst_below(node.path)
        entry = None if bypass else self.cache.get(state)
        if entry is not None:
            ancestors = {(d, s) for d, s, _ in new_chain}
            if not ((entry.states - {state}) & ancestors):
                node.children = [_clone(t, node.path, node.qty) for t in entry.children]
                diags.extend(_rebase_diag(d, node.path) for d in entry.diags)
                return
            # 缓存条目与当前祖先链冲突: 放弃复用, 现场展开
        sub: list[Diag] = []
        self._expand_children(node, new_chain, sub)
        diags.extend(sub)
        if bypass or any(d.code == "CYCLE_ACTIVE" for d in sub):
            return  # 含环判定结果或路径相关覆盖的子树不进缓存
        templates = []
        states = {state}
        for c in node.children:
            t = _normalize(c, node.path, node.qty)
            templates.append(t)
            states |= _states_of(t)
        rel_diags = []
        for d in sub:
            if d.path is not None and d.path[:len(node.path)] == node.path:
                rel_diags.append(Diag(d.code, d.severity, d.message,
                                      d.path[len(node.path):], d.basis))
        self.cache[state] = _CacheEntry(templates, rel_diags, frozenset(states))


def _rebase_diag(d: Diag, base_path: tuple[str, ...]) -> Diag:
    return Diag(d.code, d.severity, d.message,
                base_path + d.path if d.path is not None else None, d.basis)
