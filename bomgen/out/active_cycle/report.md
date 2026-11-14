# 配置BOM 报告

- 根定义: `product`　根发生: `root`　配置: `active_cycle`
- 根作用域选项: finish="steel", interface="A", recursive_service=true, service=true, turbo=false
- 实例覆盖: `root.left` → {"turbo": true}
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
　- `right` [module] 累计×2　边覆盖: {"finish": "painted"}
　　- `drive` [drive] 累计×2
　　　- `bolt` = 零件 **bolt** ×8 piece
　　　- `motor_a` = 零件 **motor_a** ×2 piece
　　- `frame` [frame] 累计×2
　　　- `coat` = 零件 **coat** ×4 piece
　　　- `shell` = 零件 **shell** ×2 piece
　- `service` [service] 累计×1
　　- `kit` = 零件 **service_kit** ×1 piece

## 物料需求(同一零件汇总, 保留各发生贡献)

| 零件 | 单位 | 总需求 | 发生贡献 |
|---|---|---|---|
| bolt | piece | 14 | `root.left.drive.bolt`×4; `root.left.turbo.bolt`×2; `root.right.drive.bolt`×8 |
| coat | piece | 4 | `root.right.frame.coat`×4 |
| motor_a | piece | 3 | `root.left.drive.motor_a`×1; `root.right.drive.motor_a`×2 |
| rotor | piece | 1 | `root.left.turbo.rotor`×1 |
| service_kit | piece | 1 | `root.service.kit`×1 |
| shell | piece | 3 | `root.left.frame.shell`×1; `root.right.frame.shell`×2 |

## 跨装配约束(在同一配置实例上检查)

- [满足] `not_both_turbo` (not_both_true)　root.left.turbo=true, root.right.turbo=false
- [满足] `ports_match` (equal_options)　root.left.drive.interface="A", root.right.drive.interface="A"

## 诊断

- [错误][CYCLE_ACTIVE] `root.service.again` 本次配置激活了定义环(相同定义+相同有效选项状态在活动祖先链上重复)　依据: {"cycle": [{"definition": "service", "path": "root.service"}, {"definition": "service", "path": "root.service.again"}]}

> 存在错误级诊断: 缺口/激活环/约束违反或未决都会阻止声明完整可制造,
> 以上已确定的发生与物料按 partial 导出, 修复后可重新构建。
