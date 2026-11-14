# 受限 PostgreSQL → SQLite 查询编译器（题 179）

把 `fixtures/queries.json` 中受限 scope 内的 PostgreSQL 报表编译为等价 SQLite
SQL，并在本机专用 PostgreSQL 16 与 SQLite 副本上用真实绑定独立核对。本实现不
封装任何现成 transpile：词法分析、递归下降语法分析、声明类型推断、SQLite 代码
生成均为手写。`sqlglot` 仅在测试里作为**独立解析交叉校验**（确认语法成立、正则
节点存在），不参与任何翻译。

## 目录

```
pg2sqlite/
  lexer.py          手写词法分析（token 携带源 offset/line/column；字符串整体切词）
  ast_nodes.py      受限 SELECT 方言 AST
  parser.py         递归下降解析；scope 外语法在原位置抛 CompileError
  types.py          逻辑类型（integer/text/boolean/null）、DDL 解析、类型推断
  distinct.py       DISTINCT ON 合法性与确定性检查（只检查，绝不追加排序）
  codegen.py        AST → SQLite SQL（窗口改写、concat 空串、三值逻辑、占位符）
  distinct_check.py 纯 Python 手工 DISTINCT ON 选行参考实现（不执行 SQL）
  verifier.py       双库装载、真实绑定、只读执行、列/类型/重复行/排序/手工选行核对
  compiler.py       compile_query / compile_report 入口
tests/              pytest 套件（29 项）
run.py              CLI：compile / verify
deliverables/       编译产物、逐查询目标 SQL、验证记录
```

## 运行

```sh
# 依赖已在 .venv（psycopg[binary]、sqlglot、pytest）
# PG16 实例：socket 在 /tmp/task179-sock，库 taskdb，已装载 fixtures/source.sql
.venv/bin/python run.py compile --out deliverables/compiled.json
.venv/bin/python run.py verify  --out deliverables/verified.json \
    --pg-socket /tmp/task179-sock --db taskdb --sqlite .runtime/target.db
.venv/bin/python -m pytest tests/ -q
```

## scope 与拒绝策略

支持：单条 SELECT、一个非递归 CTE、WHERE、ORDER BY（含 NULLS FIRST/LAST）、
DISTINCT ON、仅文本 `concat(...)`、`IS [NOT] DISTINCT FROM`、`$n` 参数、普通
比较/算术/布尔/NULL 表达式、CAST、CASE、IN、BETWEEN、LIKE。类型限整数、文本、
布尔与 NULL。

拒绝即跳过该报表并继续其余报表，错误带源 SQL 原位置与 caret 指示：

| 报表 | 结果 | rule |
|---|---|---|
| distinct_latest | compiled | — |
| cte_parameters | compiled | — |
| nullsafe | compiled | — |
| concat_nulls | compiled | — |
| nondeterministic | **rejected** | `nondeterministic_distinct`（ORDER BY 分组键后无 event_id） |
| unsupported_regex | **rejected** | `unsupported_syntax`（`~` 正则，定位到第 41 列） |

JOIN、多表、GROUP BY、集合操作、普通 DISTINCT、递归 CTE、窗口 OVER、聚合/未知
函数、`||`、ILIKE、SELECT *、ORDER BY 序数等同样在原位置拒绝。

## 关键改写语义与依据

### 1. DISTINCT ON → ROW_NUMBER 窗口子查询

```sql
-- 源
SELECT DISTINCT ON (customer_id) customer_id, event_id, label
FROM events
ORDER BY customer_id, tick DESC NULLS LAST, event_id DESC
```
改写为内层 `ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY <完整源排序>)`，
外层 `WHERE rn__rank = 1`。

- 分组键必须与 ORDER BY 最左前缀结构等价，否则 `invalid_ordering`；
- **确定性要求**：分组键之后的 ORDER BY 必须含 `event_id`（主键、全局唯一）。
  不满足报 `nondeterministic_distinct`，编译器**不会**自行追加排序键改变源查询；
- 排名列 `rn__rank` 与附加排序列（如 `tick_ord`）只存在于内层子查询，最终
  输出严格保持源 SELECT 的列与列顺序；
- 外层复用完整源 ORDER BY（含 NULLS 位置）保留最终展示顺序；
- 窗口函数与投影同层、SQLite 不允许窗口 ORDER BY 引用同层别名，因此窗口内用
  原始表达式，别名只供外层排序使用。

### 2. concat —— NULL 即空串，不传播 NULL

`concat(a,b,...)` 只接受声明为 text 的参数（非文本类型在类型检查阶段拒绝）。
字面量透传，其余参数改写为 `COALESCE(CAST(x AS TEXT), '')`，再以 `||` 连接。
不使用会传播 NULL 的裸连接符替代。

### 3. IS DISTINCT FROM —— 显式三值逻辑

不直接使用 SQLite 的 `IS ?`：实测无类型绑定 NULL 经 `IS ?` 比较会被当作未知值
而得到错误结果。改写为纯 AND/OR 三值逻辑：

```sql
((l IS NULL AND r IS NOT NULL)
 OR (l IS NOT NULL AND r IS NULL)
 OR (l IS NOT NULL AND l <> r))
```

两个 NULL 判为相等、单侧 NULL 判为不等、布尔按 0/1 比较，真值表与 PostgreSQL
完全一致。`IS NOT DISTINCT FROM` 整体取反。参数在三次引用处独立渲染，每个 `?`
都在绑定映射中登记同一 `$n` 的同一值。

### 4. 参数迁移

- `$n` 按语法 token 出现次序迁移为 `?`；同一 `$n` 多处引用即多个 `?`，绑定同一
  值；字符串字面量整体切词，`'$1'` 这样的文本永远不会被参数化；
- 绑定值按声明类型校验：bool 不被当作 int、int 不接受 bool；布尔在 SQLite 物理
  绑定为 0/1，不经过字符串化；
- PostgreSQL 侧对 NULL 参数无法推断类型，采用 `%s::声明类型` 显式标注，仍为真实
  绑定而非内联常量。

### 5. 布尔与类型

SQLite 布尔列物理为 INTEGER 0/1（DDL 带 CHECK），结果读取时按输出 schema 的
声明逻辑类型映回 Python `True/False`；比较与排序全程使用原生整数/文本，不把
列整体转字符串。两端 DDL 交叉校验列顺序与逻辑类型（SQLite 的 INTEGER 布尔列
对应 PG 的 BOOLEAN）。

### 6. CTE 名称与列顺序

CTE 名称、可选输出列名清单与内部 SELECT 列顺序原样保留；外层仅按名称引用。
DISTINCT ON 改写产生的辅助排名列不会穿透 CTE 边界。CTE 内非列表达式必须显式
别名，以固定内外名称。

## 验证方法（不只相信两个包装器返回相同 JSON）

1. **双库同数据**：PG 执行 `source.sql`；SQLite 用 `target.sql` 建表后以参数化
   INSERT 装入 `rows.json` 同一批 6 行；
2. **真实绑定、只读执行**：两侧均用参数占位符绑定，SQLite 以 `mode=ro` 打开、
   PG 设 `SET TRANSACTION READ ONLY`，已实测 INSERT/UPDATE/DELETE/DDL 均被拒；
3. **独立核对**：列名与列顺序、逻辑类型、逐行内容（含顺序与重复行）、显式
   ORDER BY（含 NULLS FIRST/LAST 四方向）；
4. **手工选行表**：`distinct_check.py` 不执行 SQL，按 PG 语义在 Python 内重放
   WHERE→排序→分组取首行→展示排序，与 PG、SQLite 结果三方逐行对照；
5. **并列审计**：distinct_latest 的 customer 1 组中 event 1/2 在 event_id 之前
   的排序键完全相同（同 tick=10），审计确认由 `event_id DESC` 唯一选出行 2。

验证记录见 `deliverables/verified.json`，4 条 compiled 全部 `verified`，2 条按
scope 拒绝。测试套件 29 项全部通过。

## 边界与非目标

不做窗口函数来源解释、schema 迁移、完整方言兼容；不支持浮点/数值转换、
locale 敏感排序，文本按二进制/码点序比较。
