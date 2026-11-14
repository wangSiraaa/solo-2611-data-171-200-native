# 配置BOM 报告

- 根定义: `product`　根发生: `root`　配置: `unknown_turbo`
- 根作用域选项: finish="steel", interface="A", recursive_service=false, service=false
- 状态: **partial — 不能发布完整BOM**(错误 3 项, 其中缺口 2 项; 已确定的发生与物料仍导出)

## 发生树(按有效配置展开, 数量沿路径相乘)

- `root` [product] 累计×1
　- `left` [module] 累计×1　边覆盖: {"finish": "steel"}
　　- `drive` [drive] 累计×1
　　　- `bolt` = 零件 **bolt** ×4 piece
　　　- `motor_a` = 零件 **motor_a** ×1 piece
　　- `frame` [frame] 累计×1
　　　- `shell` = 零件 **shell** ×1 piece
　- `right` [module] 累计×2　边覆盖: {"finish": "painted"}
　　- `drive` [drive] 累计×2
　　　- `bolt` = 零件 **bolt** ×8 piece
　　　- `motor_a` = 零件 **motor_a** ×2 piece
　　- `frame` [frame] 累计×2
　　　- `coat` = 零件 **coat** ×4 piece
　　　- `shell` = 零件 **shell** ×2 piece

## 物料需求(同一零件汇总, 保留各发生贡献)

| 零件 | 单位 | 总需求 | 发生贡献 |
|---|---|---|---|
| bolt | piece | 12 | `root.left.drive.bolt`×4; `root.right.drive.bolt`×8 |
| coat | piece | 4 | `root.right.frame.coat`×4 |
| motor_a | piece | 3 | `root.left.drive.motor_a`×1; `root.right.drive.motor_a`×2 |
| shell | piece | 3 | `root.left.frame.shell`×1; `root.right.frame.shell`×2 |

## 跨装配约束(在同一配置实例上检查)

- [未决] `not_both_turbo` (not_both_true)　依据: {"left": {"detail": {"message": "选项 'turbo' 在该发生上未赋值", "reason": "option_unbound"}, "option": "turbo", "path": "root.left", "status": "unknown"}, "right": {"detail": {"message": "选项 'turbo' 在该发生上未赋值", "reason": "option_unbound"}, "option": "turbo", "path": "root.right", "status": "unknown"}}
- [满足] `ports_match` (equal_options)　root.left.drive.interface="A", root.right.drive.interface="A"

## 诊断

- [错误][CONSTRAINT_PENDING] 跨装配约束 not_both_turbo(not_both_true) 未决: 引用路径未发生或取值未知　依据: {"left": {"detail": {"message": "选项 'turbo' 在该发生上未赋值", "reason": "option_unbound"}, "option": "turbo", "path": "root.left", "status": "unknown"}, "right": {"detail": {"message": "选项 'turbo' 在该发生上未赋值", "reason": "option_unbound"}, "option": "turbo", "path": "root.right", "status": "unknown"}}
- [错误][GAP_UNKNOWN_CONDITION] `root.left.turbo` 启用条件未确定(unknown), 该边不展开并记缺口, 不能发布完整BOM　依据: {"unknown_options": ["turbo"], "when": {"eq": [{"option": "turbo"}, true]}}
- [错误][GAP_UNKNOWN_CONDITION] `root.right.turbo` 启用条件未确定(unknown), 该边不展开并记缺口, 不能发布完整BOM　依据: {"unknown_options": ["turbo"], "when": {"eq": [{"option": "turbo"}, true]}}

> 存在错误级诊断: 缺口/激活环/约束违反或未决都会阻止声明完整可制造,
> 以上已确定的发生与物料按 partial 导出, 修复后可重新构建。
