# map-reaction：教学级 RXN 原子映射

为缺少 atom-map 的小反应 RXN 建立反应物→产物重原子双射，并定位键变化。
Python 3.11 + RDKit（`rdkit==2024.3.6`），不调用任何现成的 reaction atom
mapper，也不使用化学评分/学习模型。

## 目标（数据整理约定，非机理发现）

在所有保持 **(原子序数, 同位素)** 的反应物→产物重原子双射中，最小化

```
Σ_{无序反应物原子对 {u,u'}} |b_R(u,u') − b_P(φ(u),φ(u'))|
```

键级只取 1/2/3，未成键按 0 计。隐式氢与形式电荷允许反应前后变化，
只保留并报告，不作为映射约束。最优映射**不代表真实反应机理**。

## 方法

| 模块 | 职责 |
| --- | --- |
| `map_reaction/reader.py` | 按 RXN/SDF 文本规则切组件，RDKit 解析原子/键/同位素/电荷，sanitize 副本推算隐式 H，来源坐标 `(side, molecule_index, atom_index)` 全程保留 |
| `map_reaction/precheck.py` | ≤9 重原子、同元素 ≤6、键级 1/2/3、无显式 H/芳香键、元素+同位素计数守恒；不满足直接报错，不丢原子 |
| `map_reaction/solver.py` | 按 (元素,同位素) 分组的**分支定界 DFS**，逐对原子分配、增量计费、严格大于才剪枝；非贪心、不看坐标。同值裁决用 RDKit canonical rank（对原子编号置换稳定），保留主映射 + 至多一份同值替代 |
| `map_reaction/changes.py` | 从映射重算断键/成键/键级变化，生成原子 charge/isotope/H 的 before/after 表 |
| `map_reaction/writer.py` | 在原 mol block 的 sanitize 副本上只写 1..N 的 atom-map，保留组件顺序、键结构、同位素、电荷、价态字段与坐标，输出 mapped RXN/SDF |
| `map_reaction/verifier.py` | **独立入口**：重新解析磁盘上的实际文件，核对 map 编号唯一性、元素/同位素/电荷/H、键结构，并从读回映射重算目标值 |
| `map_reaction/cli.py` | `map` / `verify` 两个子命令 |

## 使用

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install pytest   # 仅测试需要

# 求解 + 写 mapped RXN/SDF/JSON 报告 + 独立读回核验
.venv/bin/python -m map_reaction.cli map fixtures/hydrolysis.rxn --out output

# 对已写出的文件做独立核验
.venv/bin/python -m map_reaction.cli verify fixtures/hydrolysis.rxn \
    output/hydrolysis.mapped.rxn 2

# 非守恒案例被拒绝，退出码 2
.venv/bin/python -m map_reaction.cli map fixtures/nonconserved.rxn --out output
```

每个输入在输出目录产生三个文件：`<name>.mapped.rxn`、`<name>.mapped.sdf`、
`<name>.report.json`（含主映射、同值替代、预检、键变化、原子属性前后值、
读回核验结果）。

## 验证

```sh
.venv/bin/python -m pytest tests/ -q
```

测试覆盖：

1. **真实 fixture 端到端**：水解、取代、对称、同位素、氧化、非守恒、
   原子编号置换版；每个守恒案例都与独立的 `itertools` 全排列暴力枚举
   最优值一致；
2. **置换不变性**：用 `Chem.RenumberAtoms` 生成多个确定性编号置换 RXN，
   目标值与化学变化口径（类型/元素/键级）保持不变；
3. **极小反应独立枚举**：内存构造的无变化/断键/成键/键级升降/同位素锁
   定/电荷变化/对称同值替代案例，全部由暴力枚举核对最优值，并验证跨组
   （C–O）键的计费正确；
4. **预检边界**：9 原子与同元素 6 上限、同位素不守恒、显式 H 与芳香键
   拒绝，且预检失败后原子仍完整保留；
5. **独立读回核验**：从实际写出的 RXN/SDF 重新解析，核对 map 编号、
   元素、同位素、电荷、隐式/显式 H 与键结构，再重算目标值。

## 边界与不做的事

* 只处理非氢重原子；显式 H、芳香键、立体化学、反应机理预测均不支持，
  遇到即按范围错误拒绝；
* 坐标只用于显示，不参与任何对应决策；
* 对称结构产生同值映射时只保留一份替代并标注歧义，不枚举全部映射，
  文件原子编号不作为唯一性证据。
