# 窗口帧解释工具

分析员发现把 `ROWS` 改成 `RANGE` 或 `GROUPS` 后滚动金额变化，需要知道**每行窗口实际用了哪些记录**。本仓库实现一个 Python 3.11 + PostgreSQL 16 的解释工具：先独立推导帧成员与函数结果，再用数据库交叉核验。

## 快速开始

```sh
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 启动/初始化独立测试库（不会接触生产库）
bash scripts/init_test_db.sh
export WINDOW_LAB_DSN='host=/tmp port=5433 dbname=window_lab'

# 推导 + 核验所有 fixture 查询，输出 JSON 证据
.venv/bin/python -m window_lab all --out evidence
```

单条查询：

```sh
.venv/bin/python -m window_lab explain --query-id ambiguous_rows --out evidence/ambiguous_rows.json
.venv/bin/python -m window_lab verify  --query-id ranges --out evidence/ranges.verify.json
```

手工帧表（比较 `ROWS`/`GROUPS`/`RANGE` 三种边界）：

```sh
.venv/bin/python -m window_lab \
  --schema fixtures/frame_modes_schema.json \
  --queries fixtures/frame_modes_queries.json \
  --init-sql fixtures/frame_modes.sql \
  all --out evidence/frame_modes
```

## 测试

```sh
.venv/bin/python -m unittest discover -s tests -t .                 # 单元测试，无需数据库
WINDOW_LAB_DSN='...' .venv/bin/python -m unittest discover -s tests -t .  # 含集成测试
WINDOW_LAB_DSN='...' .venv/bin/python -m tests.matrix              # 150 个窗口规格 × 5 函数的对照
```

## 目录

| 路径 | 内容 |
| --- | --- |
| `window_lab/sqlparse.py` | 严格解析与窗口继承、帧合法性校验 |
| `window_lab/engine.py` | 分区/排序/peer 组重建，三种帧模式独立推导，函数求值，不确定性建模 |
| `window_lab/db.py` | 专用测试库连接、种子装载、原始行读取、原 SQL 执行 |
| `window_lab/verify.py` | 先推导后核验；确定值逐一核对，不确定值检查范围/集合 |
| `window_lab/cli.py` | `explain` / `verify` / `all` 子命令 |
| `fixtures/` | 题目输入 + `frame_modes.sql` 手工帧表 |
| `tests/` | 单元、集成、插入顺序不变性、语义矩阵 |
| `docs/method.md` | 方法说明（支持范围、三种边界差异、NULL/空帧语义、证据格式） |

## 设计要点

- **数据库不替代解释**：帧成员由 `engine.py` 自行推导；PostgreSQL 只用于载入原始行和执行原查询核对。
- **三种边界绝不混同**：`ROWS` 按位置、`GROUPS` 按 peer 组、`RANGE` 按排序值。
- **不确定就报告不确定**：非唯一排序导致 `ROWS` 帧/值函数不明确时，输出可能范围/候选集合，不追加 `event_id`、不宣称解释了被改写的 SQL。
- **证据可回溯**：每个结果单元格带源行、分区、peer 组、帧头/尾位置与依据、实际帧成员和函数计算。

详见 [`docs/method.md`](docs/method.md)。
