# 配置BOM 报告

- 根定义: `product`　根发生: `root`　配置: `both_turbo`
- 根作用域选项: finish="steel", interface="A", recursive_service=false, service=false, turbo=false
- 实例覆盖: `root.left` → {"turbo": true}; `root.right` → {"turbo": true}
- 状态: **partial — 不能发布完整BOM**(错误 1 项, 其中缺口 0 项; 已确定的发生与物料仍导出)

## 发生树(按有效配置展开, 数量沿路径相乘)

- `root` [product] 累计×1
　- `left` [module] 累计×1　边覆盖: {"finish": "steel"}　实例覆盖: {"turbo": true}
　　- `drive` [drive] 累计×1
　　　- `bolt` = 零件 **bolt** ×4 piece
　　　- `motor_a` = 零件 **motor_a** ×1 piece
　　- `frame` [frame] 累计×1
　　　- `shell` = 零件 **shell** ×1 piece
　　- `turbo` [turbo] 累计×1
　　　- `bolt` = 零件 **bolt** ×2 piece
　　　- `rotor` = 零件 **rotor** ×1 piece
　- `right` [module] 累计×2　边覆盖: {"finish": "painted"}　实例覆盖: {"turbo": true}
　　- `drive` [drive] 累计×2
　　　- `bolt` = 零件 **bolt** ×8 piece
　　　- `motor_a` = 零件 **motor_a** ×2 piece
　　- `frame` [frame] 累计×2
　　　- `coat` = 零件 **coat** ×4 piece
　　　- `shell` = 零件 **shell** ×2 piece
　　- `turbo` [turbo] 累计×2
　　　- `bolt` = 零件 **bolt** ×4 piece
　　　- `rotor` = 零件 **rotor** ×2 piece

## 物料需求(同一零件汇总, 保留各发生贡献)

| 零件 | 单位 | 总需求 | 发生贡献 |
|---|---|---|---|
| bolt | piece | 18 | `root.left.drive.bolt`×4; `root.left.turbo.bolt`×2; `root.right.drive.bolt`×8; `root.right.turbo.bolt`×4 |
| coat | piece | 4 | `root.right.frame.coat`×4 |
| motor_a | piece | 3 | `root.left.drive.motor_a`×1; `root.right.drive.motor_a`×2 |
| rotor | piece | 3 | `root.left.turbo.rotor`×1; `root.right.turbo.rotor`×2 |
| shell | piece | 3 | `root.left.frame.shell`×1; `root.right.frame.shell`×2 |

## 跨装配约束(在同一配置实例上检查)

- [违反] `not_both_turbo` (not_both_true)　依据: {"left": {"option": "turbo", "path": "root.left", "status": "ok", "value": true}, "right": {"option": "turbo", "path": "root.right", "status": "ok", "value": true}}
- [满足] `ports_match` (equal_options)　root.left.drive.interface="A", root.right.drive.interface="A"

## 诊断

- [错误][CONSTRAINT_VIOLATED] 跨装配约束 not_both_turbo(not_both_true) 在同一配置实例上被违反　依据: {"left": {"option": "turbo", "path": "root.left", "status": "ok", "value": true}, "right": {"option": "turbo", "path": "root.right", "status": "ok", "value": true}}

> 存在错误级诊断: 缺口/激活环/约束违反或未决都会阻止声明完整可制造,
> 以上已确定的发生与物料按 partial 导出, 修复后可重新构建。
