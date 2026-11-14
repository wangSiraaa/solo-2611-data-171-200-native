# bomgen — 离线配置BOM生成器

读取装配定义、选项声明、跨装配约束与销售配置, 按**有效配置**展开实际发生树,
输出带发生路径的物料需求, 并给出阻断发布的诊断。Python 3.11, 无第三方依赖。

## 运行

```sh
cd /workspace/bomgen

# 构建: 展开 + 约束检查 + 汇总 + 自核对, 输出五个文件到 --out 目录
python3.11 -m bomgen build \
  --definitions /workspace/fixtures/definitions.json \
  --options     /workspace/fixtures/options.json \
  --constraints /workspace/fixtures/constraints.json \
  --config      /workspace/fixtures/configs/valid.json \
  --out         out/valid

# 独立核对: 从 occurrences 重新累加, 比对 parts 汇总
python3.11 -m bomgen verify --result out/valid/result.json

# 测试(14 项不变量)
python3.11 tests/test_bomgen.py
```

`build` 退出码: `0` 完整可发布, `3` partial(存在阻断诊断), `2` 输入错误。
产物: `result.json`(全量)、`occurrences.json`、`parts.json`、`diagnostics.json`、
`report.md`(可读报告, 同时打印到 stdout)。

六个材料的构建结果在 `out/<配置名>/` 下: `valid`、`inactive_cycle` 完整可发布;
`both_turbo`(双模式同开)、`conflicting_ports`(接口不一致)、`unknown_turbo`
(未定选项缺口)、`active_cycle`(激活环)为 partial —— 已确定的发生与物料仍导出。

## 语义要点(与 fixtures/semantics.md 对齐)

- **条件**: 仅 `eq / all / any / not / option` 的 JSON 数据, 不执行任意代码;
  无 `when` 视为 true。三值强 Kleene: 缺失选项为 `UNKNOWN`, **不默认 false**,
  否则可能需要的物料会被藏掉。条件求值只读当前作用域的常量, 不触发其他条件
  求值, 因此条件不可能自引用(另有深度上限兜底), 不会无限递归。
- **选项作用域**: 根选项 → 祖先继承 → 边 `overrides` → 该路径
  `instance_overrides`, 依次覆盖; 覆盖都是常量。边覆盖只作用于**该次发生及
  后代**, 不影响自己的 `when`(在父作用域求值), 从语义上杜绝条件自引用。
- **展开**: 只展开确定启用的边; 确定不启用的边直接跳过; 未确定(unknown)的边
  不展开, 记 `GAP_UNKNOWN_CONDITION` 缺口与受影响路径。数量沿路径相乘, 同一
  零件汇总时保留各发生贡献。
- **状态**: 无错误级诊断为 `complete`(可发布); 否则 `partial` —— false 与
  unknown 都阻止声明完整可制造, 但已确定的发生与物料仍导出。

## 循环状态与缓存键(关键设计)

- **循环状态**: 当前活动祖先链上的 `(定义id, 有效选项状态)` 序列。相同
  `(定义, 选项状态)` 在链上重复 = 本次已激活路径构成环 → `CYCLE_ACTIVE`,
  只阻断该边。状态按路径维护(进入压栈、离开弹栈), **不是全局 visited**:
  同一定义在兄弟位置复用会照常完整展开(`test_reuse_is_not_cycle`)。选项域
  有限 ⇒ 链上状态数有限 ⇒ 递归必然终止。定义图中的潜在环(如
  `service→service`)未被条件激活时不阻断(`inactive_cycle` 可发布)。
- **缓存键**: `(定义id, 有效选项状态的规范化形式)` —— 与循环状态同构。
  绝不只按定义编号缓存: 同一 `module` 在 `finish=steel/painted` 下是不同
  实例, 按定义编号缓存会串配置。缓存值是**相对子树**(路径相对、数量按
  子树根 qty=1 归一、诊断路径相对), 复用时重定前缀与数量。两类不缓存:
  ①子树含激活环诊断(环判定依赖祖先链); ②子树下方存在实例覆盖路径(覆盖
  按精确路径生效)。复用前还要求子树内部状态集与当前祖先链无交, 否则现场
  展开, 保证环判定不被缓存绕过(`test_cache_does_not_hide_cycle`)。

## 跨装配约束

`equal_options` / `not_both_true` 引用两个点分发生路径的有效选项, 在**同一
配置实例**的实际发生树上检查。引用路径未发生或取值未知 → `pending`, 依据中
标明是哪一侧、哪条边、条件不成立(`condition_false`)还是条件未确定
(`condition_unknown`)/选项未赋值(`option_unbound`)。`violated` 与 `pending`
都阻断发布 —— 不允许各子树独立验证通过就宣布整体可制造。

## 不变量与核对(见 tests/test_bomgen.py)

1. 每个汇总零件可由发生贡献重新累加(`verify` 子命令 + 六个配置全测);
2. 交换定义输入顺序(含子项顺序)不改变规范结果(输出全部规范化排序);
3. 改变一个局部选项只影响其作用域及相关约束(实例覆盖与边覆盖各测一例);
4. 共享子装配在不同局部覆盖下不串配置; 复用 ≠ 环; 缓存命中与现场展开一致。

## 范围

单个产品、十余定义、少量布尔/枚举选项; 不含库存、价格、排程、自动寻找最优
配置与复杂单位换算(零件自带统一计量单位, 同零件单位必然一致)。
