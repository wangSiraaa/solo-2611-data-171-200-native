# dimcheck — 工程文本量纲一致性检查器

对 `.dims` 量纲声明与 `.formula` 命名公式做**带源位置**的词法/语法分析，把
加减相容、乘除、有理字面量幂、受限函数与等号关系编译成
`M / L / T / Theta` 四维**精确有理**线性约束，再用 Gauss-Jordan 消元联合求解，
输出：

* 未知量纲的**已知 / 唯一推断 / 带自由维数**三类结论与参数化解；
* 标量自由度（零空间秩 × 4）与每个结论依赖的来源 statement；
* 矛盾时的**可复算线性组合证书**、源 span，以及按 statement 计的
  **包含极小矛盾集合**（删其中任一条即相容，并逐条复核）。

纯 Python 3.11 标准库实现，**不使用 `eval`，不依赖任何符号数学平台**。

---

## 1. 文件格式

### 1.1 `.dims` 量纲声明

每个非空、非注释逻辑行一条声明：

```
name : dimension_product
```

* `name`：ASCII 标识符 `[A-Za-z][A-Za-z0-9_]*`。
* `dimension_product`：`1`（无量纲），或以空白分隔的基础维数因子，
  每个因子可带幂。
* 每个变量可以没有声明（表示未知维数）；**跨文件同名变量共用同一个未知
  向量**，重复且不一致的声明会形成可定位的矛盾，不会各自局部推断。

```text
# 示例
mass : M
energy : M L^2 T^-2
root : L^(1/2)
angle : 1
temp : Theta
```

### 1.2 `.formula` 命名公式

每个逻辑行一条等式，标签唯一：

```
label : expr = expr
```

注释以 `#` 到行末。行列从 1 开始，列按 UTF-8 解码后的 **Unicode 码点**计；
所有 span 为半开区间 `[start_offset, end_offset)`。

---

## 2. 受限表达式语法（EBNF）

```ebnf
statement      := IDENT ":" additive "=" additive

additive       := multiplicative (("+" | "-") multiplicative)*
multiplicative := unary (("*" | "/") unary)*
unary          := ("+" | "-") unary | power
power          := primary ("^" exponent)?
exponent       := ["+" | "-"] NUMBER
                | "(" ["+" | "-"] NUMBER ["/" NUMBER] ")"
primary        := NUMBER
                | IDENT ["(" additive ("," additive)* ")"]
                | "(" additive ")"
```

### 2.1 优先级（从低到高）

1. 括号 / 函数调用
2. 字面量幂 `^`
3. 前置一元 `+` / `-`
4. 乘 `*` / 除 `/`
5. 加 `+` / 减 `-`

一元负号**低于**幂：`-x^2` 解析为 `-(x^2)`，不是 `(-x)^2`。

### 2.2 幂的限制

* 指数只能是**字面量**：带符号整数（`x^-2`）或括号内有理数
  （`x^(1/2)`、`y^(-2/3)`），分母必须为正整数。
* **变量指数**（如 `x^time`）是语法错误 `E101`，定位到该指数记号。
* 不接受一般表达式作指数。

### 2.3 受限函数表

| 函数 | 参数数 | 量纲规则 |
| --- | --- | --- |
| `exp` `log` `sin` `cos` | 1 | 输入与输出都**必须无量纲** |
| `sqrt` | 1 | 输出量纲 = 输入量纲 × `1/2` |

未知函数（如 `mystery(x)`）能被语法解析为调用节点，但在语义编译阶段以
`E300` 拒绝整条公式，不会被静默当作无量纲。参数数目不符为 `E301`。

### 2.4 字面量与零

纯数（包括 `0`）一律无量纲。**不存在多态的量纲零**：`a + 0` 中若 `a` 有量纲，
则因加减相容性而矛盾，而不是把 `0` 当成与 `a` 同维。

工具不检查数值定义域（如 `log` 的正数条件），因为不做任何数值求值。

---

## 3. 量纲语义（如何编译成线性约束）

量纲是四维有理向量 `(M, L, T, Theta)`。每个表达式节点绑定一个未知向量；
命名变量复用全局同名向量，临时节点取内部名 `t0, t1, ...`。

| 运算 / 关系 | 约束 |
| --- | --- |
| 字面量 | 节点向量 = 0（无量纲） |
| 一元 `+` / `-` | 结果向量 = 操作数向量 |
| `a + b` / `a - b` | `dim(a) = dim(b)`，且结果取相同维数（**加减相容**） |
| `a * b` | `dim(结果) = dim(a) + dim(b)` |
| `a / b` | `dim(结果) = dim(a) - dim(b)` |
| `a ^ (p/q)` | `dim(结果) = (p/q) · dim(a)` |
| `exp/log/sin/cos` | 输入向量 = 0，输出向量 = 0 |
| `sqrt(a)` | `dim(输出) = 1/2 · dim(a)` |
| 等号 `lhs = rhs` | `dim(lhs) = dim(rhs)` |
| 声明 `name : d` | `dim(name) = d`（非齐次，d 为已知向量） |

每条约束展开成 M/L/T/Theta 四个标量行，系数为精确 `fractions.Fraction`。

* **循环公式**（如 `speed = distance/duration` 与 `distance = speed*duration`）
  是正常的**联立约束**，不作为程序依赖环拒绝。
* **欠定变量不会被默认为无量纲**：保留为零空间自由参数。

---

## 4. 求解与结论分类

四个分量各自做两阶段 Gauss-Jordan（前向消元 + 回代），行变换全程维护
**来源 provenance**（约束 id 与线性组合系数），因此矛盾行能回溯到具体 span。

相容时给出：

* 一个**特解**（自由变量取 0，主元变量取 RHS）；
* **齐次零空间基**，构成可复算的参数化解
  `dim(x) = 特解(x) + Σ k_i · 基_i(x)`。

变量分类：

| 状态 | 含义 |
| --- | --- |
| `declared` | 由 `.dims` 声明给定 |
| `uniquely_inferred` | 无声明，但零空间系数全为 0，维数唯一确定 |
| `free` | 仍带自由维数，给出参数化表达式，不默认无量纲 |

**自由度**：标量自由度 = 4 × 零空间秩。仅在临时节点上非零、不触及任何命名
变量的零空间基会被剔除（那是孤立内部节点的自由度，不外显为命名变量参数）。

### 矛盾时

* **不输出任何貌似确定的单位**，变量仅登记来源，状态为 `unresolved_conflict`。
* 每个冲突给出一条 `0 = c ≠ 0` 的**线性组合证书**，并由独立重放
  （从原始标量行重新组合）核对残差非零、LHS 为零向量。
* 给出一组**包含极小**矛盾 statement 集合：对集合内每条 statement，
  从**整个系统**删除它后系统即相容（逐条复核，结果随报告返回）。
  不承诺全局最少删改，也不穷举所有矛盾集合。

---

## 5. 诊断码

| 码 | 类别 | 含义 |
| --- | --- | --- |
| `E001` | parse_error | 词法：无法识别的字符 |
| `E100` | parse_error | 公式：期望数字/标识符/括号 |
| `E101` | parse_error | 幂指数非字面量（如变量指数） |
| `E102` | parse_error | 有理指数分母非正 |
| `E103` | parse_error | 缺少期望记号（冒号、等号、括号等） |
| `E104` | parse_error | 公式末尾多余记号 |
| `E110` | parse_error | 公式标签重复 |
| `E200` | parse_error | 声明：缺少期望记号 |
| `E201` | parse_error | 声明 RHS 出现非 M/L/T/Theta 因子 |
| `E202` | parse_error | 声明有理指数分母非正 |
| `E203` | parse_error | 无量纲 `1` 后仍出现因子 |
| `E300` | semantic_error | 未知函数（不在 exp/log/sin/cos/sqrt） |
| `E301` | semantic_error | 函数参数数目错误 |
| — | dimension_conflict | 求解出的量纲矛盾（含线性组合证书与极小集合） |

解析/语义失败与量纲矛盾是**分开的两类**：被拒绝的语句不参与量纲求解，
其余语句照常分析；工具**不会自动修改原公式**。

---

## 6. 用法

### 命令行

```sh
# 人类可读说明（退出码：0 相容；1 存在矛盾或被拒绝语句）
python -m dimcheck fixtures/known.dims fixtures/model.formula

# 结构化 JSON
python -m dimcheck --json fixtures/tiny.dims fixtures/tiny.formula

# 目录会递归收集其中的 .dims / .formula
python -m dimcheck fixtures/
```

### Python API

```python
from dimcheck import check, explain, to_dict

result = check(["path/to/a.dims", "path/to/b.formula"])
print(explain(result))          # 中文说明文本
payload = to_dict(result)       # JSON 可序列化结构
print(result.status)            # consistent / inconsistent / parse_error_only
```

`check(paths, sources=None)` 接受文件或目录；`SourceStore` 持有全部源文本，
供 span 回溯原片段。报告中的 `variables`、`conflicts`、`diagnostics`、
`rejected_items` 均为结构化数据，含 1-based 行、码点列与半开 offset。

### 解析树与结构化诊断（库级）

```python
from dimcheck import SourceStore, parse_formula_file, parse_dims_file, DiagnosticBag

store = SourceStore()
source = store.add("f.formula", "l: y = x^(1/2)\n")
formulas = parse_formula_file(source, DiagnosticBag())   # 带 span 的 AST
```

AST 节点（`Number / Var / Unary / Binary / Power / Call / FormulaStmt /
DeclStmt`）均携带 `Span`。

---

## 7. 输出结构（JSON 摘要）

```jsonc
{
  "status": "consistent | inconsistent | parse_error_only",
  "base_dimensions": ["M", "L", "T", "Theta"],
  "diagnostics": [ { "code", "severity", "message", "location", "snippet" } ],
  "variables": [
    {
      "name": "velocity",
      "status": "declared | uniquely_inferred | free | unresolved_conflict",
      "dimension": ["0", "1", "-1", "0"],
      "dimension_text": "L T^-1",
      "parametric": null | { "particular", "parameters",
                             "per_base_component", "text" },
      "depends_on_items": ["...#formula#speed", "..."],
      "sources": [ { "file", "line", "column", "...": "..." } ]
    },
    { "name": "__degrees_of_freedom__", "nullspace_rank": 2,
      "scalar_degrees_of_freedom": 8 }
  ],
  "conflicts": [
    { "kind": "linear_combination_conflict", "base_dimension": "T",
      "equation", "residual", "replay_residual", "replay_matches": true,
      "combination": [ { "coefficient", "item_id", "span", "snippet" } ] },
    { "kind": "minimal_conflict_set", "item_ids": ["..."],
      "minimality_checks": [ { "removed_item",
        "consistent_after_removal": true } ] }
  ],
  "rejected_items": [ { "item_id", "reason", "span", "snippet" } ]
}
```

---

## 8. 明确的非目标

本工具**不做**以下事情：

* 不做数值方程求解或表达式数值求值；
* 不做单位换算（如米↔英尺）；
* 不做温标转换或温标偏移（如摄氏↔开尔文），温度只作为 `Theta` 维数参与；
* 不检查数值定义域（`log` 正负、除零等）；
* 不自动改写、修正或补全原公式；
* 不承诺最少删改，也不穷举所有矛盾集合，只给一组可核验的包含极小集合。

---

## 9. 测试

```sh
python -m unittest discover -s tests -t .
```

测试覆盖：带位置词法/语法与全部受限语法拒绝；tiny 材料手工核对
`speed = L T^-1`；物理关系链、自由增益、循环联立、有理幂、sqrt、
无量纲函数、温度同维比较；把特解与零空间基（含任意有理参数组合）**独立代回
全部约束**；冲突集合逐条移除复核极小性；线性组合证书非零残差重放；
文件排列置换下数学结论不变、源位置仍准确；跨文件同名变量共用维数；
解析失败与量纲矛盾分离。
