# 小型指标查询规划器 (SQLite / Python 3.11)

按模型声明把 JSON 指标查询编译成"按事实分别聚合、再按键拼接"的执行计划并执行。
核心目标只有一个：**订单金额、明细金额、实收款永不互相放大**。

不解析任意 SQL，不建网页，不使用第三方库。

---

## 1. 快速开始

```sh
# 1) 建库（从给定 SQL 脚本重建）
python3 -m planner build --sql fixtures/schema_and_data.sql --db .runtime/task.db

# 2) 校验模型结构与声明的基数（执行前必做）
python3 -m planner verify --db .runtime/task.db --model fixtures/model.json

# 3) 运行查询，写出 CSV/JSON/计划
python3 -m planner run --db .runtime/task.db --model fixtures/model.json \
  --query fixtures/queries/region.json --out results/region --show

# 4) 只看执行计划（不做聚合，只读一次订单号半连接）
python3 -m planner explain --db .runtime/task.db --model fixtures/model.json \
  --query fixtures/queries/combined.json

# 5) 查看每一事实能到达哪些维度
python3 -m planner inspect --db .runtime/task.db
```

`run --out PREFIX` 生成四个文件：

| 文件 | 内容 |
| --- | --- |
| `PREFIX.csv` | 结果表（维度 + 指标 + 基础计数/分母列） |
| `PREFIX.json` | `{columns, rows, plan}`，机器可读 |
| `PREFIX.plan.json` | 完整执行计划（结构化） |
| `PREFIX.plan.txt` | 面向复核者的中文/英文可读计划 |

错误一律以**非零退出码 + 结构化说明**返回（`--json` 时输出 JSON）。
三类错误码：`validation`（结构非法）、`unplannable`（缺少分摊规则）、
`cardinality_violation`（数据违反声明基数，附关键记录）。

---

## 2. 语义设计（为什么不会重复计量）

### 2.1 每事实独立聚合

每个基础指标都来自**唯一一个事实**（`model.metrics[*].fact`）。规划器为每个
用到的事实各生成一条 SQL：

```sql
FROM <fact> AS t0  ... GROUP BY <各维度>
```

三条事实（`orders` / `lines` / `payments`）**从不出现在同一条 SELECT 的 FROM
里**。所谓"连表放大金额"正是把三者放进一张宽表造成的；本实现从结构上排除了它。

事实表之间只允许沿声明的 `many_to_one` 做**维度查找**（例如 `lines -> orders`
读 `order_date`），且聚合函数只作用于该任务自己的 `t0` 列
（`SUM(t0.line_amount)`），绝不 SUM/COUNT 另一事实。

### 2.2 维度可达性

维度只能沿声明的 `many_to_one` **正向**走：

```
lines    -> orders -> customers        => region, day
lines    -> products                   => category
payments -> orders -> customers        => region, day
payments                               => method
orders   -> customers                  => region;   day
```

反向（`one_to_many`，如 `orders -> lines`）**不是维度路径**：若允许回连，
一条订单会按明细行数被复制，`booked` 就会放大。

### 2.3 两种过滤作用域

| scope | 含义 | SQL 形态 |
| --- | --- | --- |
| `local` | 只影响**所属事实**的参与行 | 只出现在该事实聚合的 `WHERE` |
| `entity_exists` | 用匹配事实选出**订单实体集**，再限制**所有指标** | 一次订单号半连接 `entity_key IN (...)` |

- 多个 `entity_exists` 取**交集**（`INTERSECT`）：订单必须同时满足每个条件。
- `entity_exists` 是"订单级"的，不是行级：例如
  `entity_exists(payments.method=bank)` 选出"存在 bank 付款的订单"，随后
  `received` 仍取该订单的**全部**付款；而 `local(payments.method=bank)` 只取
  bank 付款行。二者口径不同。
- **local 绝不沿关联传播**。`local(lines.category=A)` 只裁明细，付款、订单指标
  不受影响（`results/local_a.csv` 中 south 的 `received=18000` 即证据）。

给定样例的两种口径对比（south 组）：

| 查询 | sales | received |
| --- | --- | --- |
| `local(lines.category=A)` | 5000（仅 A 明细） | 18000（全部付款，未受 lines 过滤影响） |
| `entity_exists(lines.category=A)` | 5000（仅 A 订单明细） | 3000（仅 A 订单 O5 的付款） |

无法规划时（例如按 `category` 拆 `received`），报错并给出**具体路径与粒度原因**：
`received` 在 payments 粒度，`category` 只在 products 上，从 payments 无
`many_to_one` 路径到达；模型未声明任何分摊规则，规划器**拒绝静默平均分配**。

### 2.4 组合规则

- 分组域 = 过滤后各事实分组键的**并集**；
- 某事实在某组缺失 → 该指标取 `empty_measure`（0），不产生重复行；
- 组只在所有指标都无贡献时省略（这种组根本不进并集）；
- 比率**在当前分组上重算** `numerator / denominator`，分母为 0 → `zero_denominator`
  （`null`），不是全局比率，也不是平均；
- 每个结果行额外保留 `*__numerator` / `*__denominator` 与 `__rows_<fact>`，
  让复核者看到基础计数与分母。

### 2.5 基数校验（执行前）

`many_to_one(src -> dst)` 要求：源行的键在目标表**唯一**，且源键都能在目标表找到。
`verify` / `run` / `explain` 都会先扫全表：

- 目标键重复 → 一个源行会匹配多行（会扇出），报 `destination_key_not_unique`
  并列出 `key`/`count`；
- 源键无目标行 → `missing_destination`；
- 事实行的实体键在 `orders` 不存在 → `orphan_entity_key`。

`fixtures/bad_cardinality_patch.sql`（在关闭外键的副本上制造重复 `C1`）被本实现
拦截，输出关键记录 `key='C1' count=2`。

---

## 3. 目录结构

```
planner/
  errors.py     四类可读错误
  model.py      模型加载、结构校验、可达性 BFS、基数校验
  planner.py    查询绑定、可达性判定、按事实编译 SQL、执行与拼接、计划文本
  api.py        Planner facade (run / compile / plan_only)
  output.py     CSV/JSON 写出、终端表格
  loader.py     建库与连接
  cli.py        build / verify / run / explain / inspect
tools/
  verify_independent.py   逐订单独立累加核验器（不复用规划器 SQL）
tests/
  test_planner.py         40 个单元/回归测试
examples/
  model_extended.json     新增指标声明（证明非硬编码）
  new_query.json          新指标查询
fixtures/                 题目给定材料（原样保留）
results/                  各查询结果与计划
```

所有查询与模型均通过**声明驱动**编译；代码中没有 `region`/`combined` 等样例名，
新增指标只需在模型里加声明（见 `examples/`）。

---

## 4. 非硬编码证明

`examples/model_extended.json` 在原有指标外新增三个**基础量比率**：
`line_share_ratio = sales/booked`、`avg_line_value = sales/units`、
`avg_payment_value = received/payment_count`；`examples/new_query.json` 组合
`method` 维度、`entity_exists(payments.method in [card,bank])` 与
`local(lines.category in [A])`。

```sh
python3 -m planner run --db .runtime/task.db --model examples/model_extended.json \
  --query examples/new_query.json --out results/new_metric --show
```

结果（`results/new_metric.csv`）与独立核验一致：

```
region  booked  sales  units  received  payment_count  line_share_ratio  avg_line_value  avg_payment_value
north   10000   6000   1      10000     3              0.6               6000.0          3333.33
south   20000   5000   1      18000     3              0.25              5000.0          6000.0
```

注意 `line_share_ratio` 的分母 `booked` 来自 `orders`：若按 `category` 分组，规划器
会正确判为**不可规划**（`orders` 无到 `category` 的路径），而 `avg_line_value`
（分子分母都来自 `lines`）则可按 `category` 规划——可达性由**每个叶子指标**决定，
不是由整条查询一刀切。

---

## 5. 核验

### 5.1 逐订单独立累加

`tools/verify_independent.py` 完全不使用规划器的 SQL：它按订单逐行手工累加，
自行实现实体集交集、局部过滤、比率重算，然后与规划器输出逐组、逐指标、
逐基础计数比对。

```sh
for q in region category_sales orders_with_a local_a combined; do
  PYTHONPATH=. python3 tools/verify_independent.py --db .runtime/task.db \
    --query fixtures/queries/$q.json
done
# 全部: OK: planner output matches independent per-order accumulation
```

### 5.2 抗放大实验

在同一订单追加**另一事实**的行，相应指标不得被意外放大：

- O1 追加一条 bank 付款 `+7777` 和一条 B 明细 `+1234, qty 1`：
  `booked` 仍 18000（orders 未变）、`order_count` 仍 3、
  `sales` 仅 `+1234`、`received` 仅 `+7777`。
- 追加一条 O1 的 B 明细（O1 已含 A 明细）：`entity_exists(category=A)` 口径下
  `booked`/`received` 不变，`sales` 仅按真实新增明细增长。
- 追加一条 O1 的 card 付款：`local(lines.category=A)` 查询的 `sales` 不变；
  `received` 变化（因为它**本就不受 lines 局部过滤约束**，这是正确口径）。
- 向被 `entity_exists(A)` 排除的 O3 追加 card 付款：`combined` 输出完全不变。

以上均作为 `TestAntiFanout` 测试固化。

### 5.3 运行测试

```sh
python3 -m unittest discover -s tests -v
# Ran 40 tests ... OK
```

覆盖：各样例查询数值、local/entity_exists 口径差异、空组/零分母/缺事实组、
不可达维度与非法过滤、嵌套比率拒绝、新指标、基数破坏、抗放大、计划结构。

---

## 6. 结果文件一览

| 文件 | 说明 |
| --- | --- |
| `results/region.{csv,json}` | 按 region 的 booked/sales/received/order_count/paid_ratio |
| `results/category_sales.*` | 按 category 的 sales/units（仅 lines 事实） |
| `results/orders_with_a.*` | `entity_exists(lines.category=A)` 限制全部指标 |
| `results/local_a.*` | `local(lines.category=A)` 只裁明细 |
| `results/combined.*` | `region,day` × entity_exists(A) × local(payments.bank) |
| `results/new_metric.*` | 扩展模型的新指标查询 |
| `results/plans/*.plan.{txt,json}` | 每查询的执行计划 |

`combined` 的一个关键行：`north / 2026-01-03` 只有 O4（金额全 0），
`sales=0`、`received=0`、`paid_ratio=null`（零分母按模型约定），
`paid_ratio__denominator=0` 仍保留，证明重算发生在**当前分组**而非全局。

---

## 7. 规模与边界

- 仅五张小表；仅 `SUM` / `COUNT` 与基础量比率；
- 不自动分摊、不做通用语义平台、不解析任意 SQL；
- 不支持的 `cardinality`（非 `many_to_one`）、嵌套比率、未知维度/指标/事实
  一律在规划阶段拒绝并说明原因。

---

## 附录：原始输入材料说明（保留自 fixtures 提供方）

> `fixtures/schema_and_data.sql` 可用 Python `sqlite3.executescript` 建库；
> `model.json` 是运行时声明；`queries/` 中有合法及无法规划的请求。
> `bad_cardinality_patch.sql` 只供在关闭外键的独立副本上制造模型基数违例。
>
> 金额单位为分。所有输出规则写在模型 `semantics` 中，基础指标不能因增加另一
> 事实行而放大。无需第三方库。
>
> 所有数据均为合成材料。实现代码、结果与独立核验由作答者完成。输入中的数字
> 和规则是业务定义，不是参考答案。

