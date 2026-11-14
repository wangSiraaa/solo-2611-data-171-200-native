# 窗口帧解释工具：方法说明

本工具回答一个具体问题：**给定一条 `SELECT`（单表、带窗口函数）和表数据，每一结果行的窗口帧实际包含哪些记录？** 它先独立推导帧成员与函数结果，再执行原查询做交叉核验；数据库只用于核对，不能替代解释。

- 语言：Python 3.11；数据库：PostgreSQL 16。
- 依赖：`psycopg[binary]`、`sqlglot`（见 `requirements.txt`）。
- 只处理 `fixtures/schema.json` 指定的单表；不产生关系级行级血缘，不做 SQL 放宽或执行计划优化。

## 1. 支持范围

| 维度 | 支持 |
| --- | --- |
| 语句 | 单个 `SELECT ... FROM <单表>`，投影为标识列 `event_id` + 窗口函数 |
| 窗口函数 | `SUM(x)`、`COUNT(*)`、`COUNT(x)`、`FIRST_VALUE(x)`、`LAST_VALUE(x)` |
| 窗口定义 | 内联 `OVER (...)`、命名 `WINDOW`、合法的窗口继承（`w AS (base ...)`）、`OVER w` 与 `OVER (w ...)` |
| 帧边界 | 显式 `ROWS` / `GROUPS` / `RANGE` 的 `BETWEEN ... AND ...` |
| `RANGE` 偏移 | 仅单整数排序键（`1 PRECEDING` 等）；无偏移的 `RANGE` 不限制键数 |
| 排序 | 多列、`ASC`/`DESC`、`NULLS FIRST`/`LAST`（省略时按 PostgreSQL 默认） |
| 禁止 | `JOIN`、`WHERE`、`GROUP BY`、`HAVING`、`DISTINCT`、`LIMIT`、集合运算、子查询、`EXCLUDE`、窗口简写（`ROWS 1 PRECEDING` 需写成显式 `BETWEEN`） |

超出范围的输入一律抛 `ParseError`，不会被静默改写。输入中已包含的 `ambiguous_rows`（非唯一 `ROWS` 排序）属于**要诊断的输入**，工具会明确报告不确定，而不是追加 `event_id` 假装确定。

## 2. 处理流程

1. **严格解析**（`window_lab/sqlparse.py`）
   - 用 sqlglot（PostgreSQL 方言）解析。bundled sqlglot 不识别 `GROUPS` 关键字，故自定义 tokenizer 仅把窗口帧里的 `GROUPS` 映射到可解析 token；`WindowSpec.kind` 仍保留原文 `GROUPS`。
   - 展开命名窗口继承，并按 PostgreSQL `transformWindowDefinitions` 规则校验：不能覆盖 `PARTITION BY`；不能覆盖已有 `ORDER BY`；不能复制带帧的窗口；引用必须指向先前定义。
   - 校验帧端点合法性（如 `UNBOUNDED FOLLOWING` 不能作起点）、`GROUPS` 必须有 `ORDER BY`、偏移 `RANGE` 必须恰好一个整数排序键。
2. **独立推导帧**（`window_lab/engine.py`）
   - 自行按 `PARTITION BY` 分组、按全部 `ORDER BY` 项排序、重建 peer 组（全部排序项都相等才算 peer；`NULL` 与 `NULL` 互为 peer）。
   - 按帧模式分别计算头/尾位置：`ROWS` 位置计数、`GROUPS` peer 组计数、`RANGE` 排序值比较。三者绝不混同。
   - 按函数语义聚合：`SUM` 忽略 `NULL`，全空帧/全 `NULL` 返回 `NULL`；`COUNT(*)` 计位置数，`COUNT(x)` 计非空；`FIRST_VALUE`/`LAST_VALUE` 只看当前帧首/尾。
3. **产出证据**（`explain`）
   - 每个结果单元格给出：源行、分区、peer 组、帧头/尾位置与依据文字、各 peer 组被完整/部分纳入的情况、帧成员、函数值及其理由。
   - 所有不确定处显式标注（见第 4 节）。
4. **交叉核验**（`window_lab/verify.py`）
   - 先完成推导，再执行原 SQL。仅对**声明为确定**的单元格要求与数据库一致；不确定单元格只检查观测值落在声明范围内。

## 3. 三种边界模式的区别（手工帧表）

`fixtures/frame_modes.sql` 提供一张与种子表结构相同、但刻意包含并列值的手工表（插入顺序已打乱）。以下仅列 `account='A'`，按 `tick NULLS LAST, event_id` 排序：

```
event_id | tick | amount
---------+------+-------
   1     |  1   |  10
   2     |  1   |  20
   3     |  3   |  30
   4     |  4   |  NULL
   5     |  4   |  40
   7     |  5   |  NULL
   8     |  5   |  80
   9     |  5   |  90
   6     | NULL |  60
```

三张手工帧表（`SUM(amount)`，帧均为 `... 1 PRECEDING AND CURRENT ROW`）。表中为工具推导并经 PostgreSQL 16 核验的值；`a–b` 表示声明为不确定的可能范围，`/NULL` 表示也可能为空：

| event_id | tick | ROWS total | GROUPS total | RANGE total |
| --- | --- | --- | --- | --- |
| 1 | 1 | *10–30* | **30** | **30** |
| 2 | 1 | *20–30* | **30** | **30** |
| 3 | 3 | *40–50* | **60** | **30** |
| 4 | 4 | *30–40* | **70** | **70** |
| 5 | 4 | *40–70* | **70** | **70** |
| 7 | 5 | *0–90 / NULL* | **210** | **210** |
| 8 | 5 | *80–170* | **210** | **210** |
| 9 | 5 | *90–170* | **210** | **210** |
| 6 | NULL | *60–150* | **230** | **60** |

（`tick` 为 `NULL` 的行排在最末，按 `NULLS LAST`。）

差异来源：

- **`ROWS`** 数位置。并列 `tick=1` 的两行谁在前不由排序决定，因此同组行、以及「帧边界切到并列组」的行都可能不确定（表中斜体为可能范围）。
- **`GROUPS`** 数 peer 组：当前组 + 前一组的所有行。`event_id=3` 的帧是 `{tick=1 组, tick=3 组}`，与 `ROWS` 的「前 1 行」不同。
- **`RANGE`** 按排序值：`tick=3` 的帧是 `[2,3]`，只含自身；`tick=4` 的帧是 `[3,4]`，含 `tick=3` 与两个 `tick=4` 行。`tick=NULL` 只与 `NULL` 比较，故 `event_id=6` 的帧只含两个 `NULL` 行。

三者的 `COUNT(*)` 帧大小也随之不同：`ROWS` 通常是 2（分区首行为 1）；`GROUPS` 为当前组加上前一组的总行数；`RANGE` 随排序值距离变化（可能为 1，也可能包含多个并列行）。完整、可机读的版本见 `evidence/` 目录下的 JSON（含 `head_basis` / `tail_basis` 文字）。

## 4. 不确定性的表达

工具区分两类不确定：

1. **部分 peer 组（所有模式都可能出现，`ROWS` 最常见）**
   帧边界落在 peer 组内部时，排序不能决定哪些行占据被纳入的位置。
   - `SUM` / `COUNT` 给出 `possible_min` / `possible_max`（及 `null_possible`），必要时给出 `possible_values`。
   - 帧成员给出 `definite_event_ids` 与 `ambiguous_groups`（组的全部 `event_id`、被纳入数量的最小/最大值）。
2. **组内位置（`ROWS`）**
   `ROWS` 帧的位置还取决于结果行自身在其 peer 组内的槽位。引擎枚举该行所有可能的槽位（枚举槽位而非全排列），若不同槽位导致不同帧/结果，标记为不确定。

对值函数 `FIRST_VALUE` / `LAST_VALUE`，即使边界落在组内，若整组在该参数列上取值相同，仍可判为确定；否则报告候选取值集合。

工具**不会**：给 SQL 追加 `event_id` 来消除歧义、声称解释了被改写的 SQL、或枚举全部可能的行排列。它只给出可证明的范围与集合。

## 5. 空帧、分区首尾与 `CURRENT ROW`

- `ROWS BETWEEN 2 FOLLOWING AND 3 FOLLOWING` 在分区尾部会产生**空帧**：`SUM` 为 `NULL`、`COUNT(*)` 为 `0`、`FIRST_VALUE`/`LAST_VALUE` 为 `NULL`（`empty` 字段为 `true`，`size` 为 `0`）。
- `CURRENT ROW` 在 `ROWS` 中是位置；在 `GROUPS`/`RANGE` 中是「当前 peer 组的起点/终点」。
- 起止颠倒、越界的偏移会按 PostgreSQL 语义裁剪（例如 `5 PRECEDING` 在分区开头裁到位置 0），裁剪过程写在 `head_basis`/`tail_basis`。

## 6. 偏移 `RANGE` 的 `NULL` 与方向语义

偏移 `RANGE` 逐行镜像 PostgreSQL `nodeWindowAgg.c` 的 `in_range` 判定：

- 当前行为 `NULL` 时，只按 `NULLS FIRST`/`LAST` 决定帧头/尾落在 `NULL` 组之前还是之后；不与数值做偏移运算。
- `DESC` 会翻转加减与比较方向；`NULLS FIRST/LAST` 独立生效。
- 整数溢出按 `in_range_int4_int4` 的溢出分支处理。
- 空值 `RANGE` 的帧边界由 peer 组决定（`CURRENT ROW` 包住整个 `NULL` peer 组）。

## 7. 运行方式

```sh
# 1) 启动独立测试库（见 environment/README.md），建库并设置连接串
export WINDOW_LAB_DSN='host=/tmp port=5433 dbname=window_lab'

# 2) 全量：推导 + 核验所有 fixture 查询，输出 JSON 证据
.venv/bin/python -m window_lab all --out evidence

# 3) 单条
.venv/bin/python -m window_lab explain --query-id rows --out evidence/rows.json
.venv/bin/python -m window_lab verify  --query-id rows --out evidence/rows.verify.json

# 4) 手工帧表（三种边界差异）
.venv/bin/python -m window_lab \
  --schema fixtures/frame_modes_schema.json \
  --queries fixtures/frame_modes_queries.json \
  --init-sql fixtures/frame_modes.sql \
  all --out evidence/frame_modes

# 5) 测试
WINDOW_LAB_DSN='...' .venv/bin/python -m unittest discover -s tests -t .
WINDOW_LAB_DSN='...' .venv/bin/python -m tests.matrix   # 与 PostgreSQL 的大矩阵对照
```

连接串必须显式指定一个专用测试库；工具拒绝 `postgres` / `template*`。数据库仅用于载入种子、读取原始行与执行原 SQL 做核验。

## 8. 证据 JSON 结构（要点）

每条查询一个文件，顶层含 `expressions`（每个窗口函数的完整窗口定义与帧）和 `results`（每个结果单元格）。单个结果单元格示例字段：

```json
{
  "event_id": 2,
  "expression": "total",
  "function": "SUM",
  "source_row": {"event_id": 2, "account": "A", "tick": 1, "amount": 20},
  "partition_index": 0,
  "peer_group": {"index": 0, "size": 2, "start_position": 0,
                 "end_position_exclusive": 2, "event_ids": [1, 2]},
  "frame": {
    "mode": "ROWS", "start_bound": "1 PRECEDING", "end_bound": "CURRENT ROW",
    "variants": [
      {"current_slot_in_peer_group": 0, "head_position": 0, "tail_position_exclusive": 1,
       "groups": [{"group_index": 0, "inclusion": "partial", "included_count": 1}]},
      {"current_slot_in_peer_group": 1, "head_position": 0, "tail_position_exclusive": 2,
       "groups": [{"group_index": 0, "inclusion": "full", "included_count": 2}]}
    ]
  },
  "members": {"deterministic": false, "definite_event_ids": [],
              "ambiguous_groups": [{"group_index": 0, "group_size": 2,
                                    "event_ids": [1, 2], "included_count_min": 1,
                                    "included_count_max": 2}]},
  "value": {"deterministic": false, "possible_min": 20, "possible_max": 30,
            "null_possible": false, "reason": "..."}
}
```

`*.verify.json` 给出逐单元格 `status`（`match` / `nondeterministic` / `mismatch` / `not_checked`）与观测值，便于从任一结果行回到原数据、窗口定义与实际帧成员。

## 9. 已验证范围

- 6 条 fixture 查询 + 5 条手工帧表查询，全部在 PostgreSQL 16 上核验通过；确定的单元格逐一相等，不确定的单元格观测值均落在声明范围内。
- 额外矩阵（`tests/matrix.py`）：6 种排序 × 25 种帧 × 5 个函数，共 150 个窗口规格、6120 个结果单元格，与 PostgreSQL 零偏差。（多列排序的偏移 `RANGE` 被解析器与 PostgreSQL 同样拒绝，不计入。）
- 补充边界矩阵（`DESC`、`NULLS FIRST`、`0` 偏移、超远偏移）：2160 个单元格零偏差。
- 插入顺序不变性：对 fixture 查询用多种物理插入顺序重排，确定结果与不确定性标记均不变；不确定输入继续标明。
