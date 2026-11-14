# 配置BOM 报告

- 根定义: `product`　根发生: `root`　配置: `valid`
- 根作用域选项: finish="steel", interface="A", recursive_service=false, service=false, turbo=false
- 实例覆盖: `root.left` → {"turbo": true}
- 状态: **完整可发布**(发生 17 项, 零件 5 种, 无阻断诊断)

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
| bolt | piece | 14 | `root.left.drive.bolt`×4; `root.left.turbo.bolt`×2; `root.right.drive.bolt`×8 |
| coat | piece | 4 | `root.right.frame.coat`×4 |
| motor_a | piece | 3 | `root.left.drive.motor_a`×1; `root.right.drive.motor_a`×2 |
| rotor | piece | 1 | `root.left.turbo.rotor`×1 |
| shell | piece | 3 | `root.left.frame.shell`×1; `root.right.frame.shell`×2 |

## 跨装配约束(在同一配置实例上检查)

- [满足] `not_both_turbo` (not_both_true)　root.left.turbo=true, root.right.turbo=false
- [满足] `ports_match` (equal_options)　root.left.drive.interface="A", root.right.drive.interface="A"

## 诊断

(无诊断)
