# 离线 Iceberg v2 行级扫描器（scan / explain）

本目录实现题目要求的只读路径：给定 `fixtures/plan.json`、真实 Iceberg v2
metadata、Avro manifest-list / manifest 与 Parquet 数据/删除文件，输出指定快照
**真正可见**的行、文件可见性表和逐行保留/删除证据。不访问 S3、catalog 或网络，
不依赖任何已有的整表 Iceberg scan 实现。

## 1. 环境

需要 Python 3.11 与依赖：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # pyarrow==17.0.0, fastavro==1.9.7
```

## 2. 运行

全部命令都在仓库根目录执行，`--plan` 指向固定映射文件。

```sh
# 单个快照；输出 rows.parquet / files.csv / rows.jsonl / plan.json
.venv/bin/python -m iceberg_offline scan \
    --plan fixtures/plan.json --snapshot 103 --out out/snapshot-103

# 仅写可独立读取的行表
.venv/bin/python -m iceberg_offline scan \
    --plan fixtures/plan.json --snapshot 102 --out out/s102 --rows-only

# 一次扫描 plan.json 中列出的所有快照，每个快照一个子目录
.venv/bin/python -m iceberg_offline scan \
    --plan fixtures/plan.json --all-snapshots --out out/all

# 从物理行回溯到快照 / manifest entry / 继承序号 / 实际删除记录
.venv/bin/python -m iceberg_offline explain \
    --plan fixtures/plan.json --snapshot 103 \
    --file s3://offline-fixture/table/data/b.parquet --row 1
```

`--file` 使用 manifest 里的**原始 URI**（不是本地路径），`--row` 是该数据文件内
**零基物理行号**（跨 row group 连续）。省略 `--snapshot` 时使用 metadata 的
`current-snapshot-id`；`explain` 必须给出 `--snapshot`。

诊断失败（不支持的格式、未映射 URI、越界行、已移除文件等）写到 stderr 并以
退出码 2 结束。

## 3. 输出格式

`scan` 非 `--rows-only` 时生成：

| 文件 | 内容 |
| --- | --- |
| `rows.parquet` | 可见行。业务列按 schema-id 0 的字段顺序，另附 `_iceberg_file_path`（原始 URI）、`_iceberg_physical_row`（零基物理行）、`_iceberg_data_sequence`、`_iceberg_file_sequence`、`_iceberg_snapshot_id`。普通 Parquet，可被任何工具独立读取。 |
| `files.csv` | 快照 manifest list 中每条 manifest entry：`status`（ADDED/EXISTING/DELETED）、`content`、`file_path`、`live`、`data_sequence`、`file_sequence`、两者是否继承、`record_count`、`entry_snapshot_id`、`manifest_entry_index`（entry 在 manifest 内的零基位置）。DELETED 与删除文件也列出。 |
| `rows.jsonl` | 每个被扫描的物理行一条 JSON：文件身份、`physical_row`、`row_group`、`row_in_group`、业务值、两个序号、`deleted`，以及 `evidence` 列表（命中的删除文件、删除文件内物理行、删除序号、position 目标路径或 equality 键字段/键值）。重复命中会有多条 evidence，但该行只计一次删除。 |
| `plan.json` | 本次快照的 manifest list、实际读取的 manifest / 数据文件 / 删除文件、行数汇总。 |

`explain` 输出 JSON：`snapshot`（快照号、序号、manifest list、operation）、
`data_file`（manifest 路径、manifest 序号、manifest entry 零基位置、状态、entry 的
`snapshot_id`、`data_sequence`/`file_sequence` 及是否从 manifest-list 继承）、`row`（物理行、
row group、行内偏移、业务值）、`visible`/`deleted`、`delete_evidence` 与资格规则。

## 4. 语义要点

- **快照选择**：只从 metadata.snapshots 取指定 `snapshot-id`，只沿该快照自己的
  `manifest-list` 读取。不合并父快照或所有历史 manifest。
- **有效文件**：manifest entry `status` 0=EXISTING、1=ADDED 为有效；2=DELETED
  只用于可见性表，不读取其数据。未被任何 manifest 引用的 Parquet 不会打开。
- **序号继承**：entry 的 `sequence_number` / `file_sequence_number` 为 null 时
  继承所在 manifest-list 条目的 `sequence_number`（仅 ADDED 允许为 null；否则
  报错）。绝不使用当前快照号覆盖。
- **两个序号**：`data_sequence` 决定删除资格；`file_sequence` 只记录文件加入时间，
  不参与删除判定。
- **position delete**：以完整 `file_path` + 零基物理行号（整个文件连续，不按
  row group 重置）定位；资格为 `data_sequence <= delete_sequence`。按顺序读取
  row group 以保证跨组连续编号。
- **equality delete**：`equality_ids` 是字段 ID（不是同名列），按这些 ID 取删除
  文件与数据文件的列构造复合键；资格为 `data_sequence < delete_sequence`。复合键
  全部相等才删除；`NULL` 与 `NULL` 视为相等。同序号数据行保留。
- **身份**：行的身份是（原始文件 URI, 物理行号），不是业务值。完全相同业务值的
  不同物理行各自判定；重复删除只移除一行一次，但 evidence 可有多条。
- **列解析**：所有列都通过 Parquet 的 `field_id` 解析（`PARQUET:field_id`），
  不按业务列名猜测。

## 5. 支持范围与边界（显式拒绝，不猜测）

支持：无分区；单一 schema（schema-id 0）、平面 long/string 字段；data、
position delete、equality delete 三种 content；PARQUET 格式；`plan.json` 的固定
URI 前缀映射。

以下结构会以 `unsupported:` 诊断明确拒绝，退出码 2：

- format-version ≠ 2；多 schema / schema 演进 / schema-id ≠ 0；snapshot schema-id ≠ 0
- 非空 partition spec、非零 spec-id，或 data_file 带非空 partition
- 嵌套/复杂字段类型、long/string 之外的字段类型
- manifest / data_file content 非 0/1/2；entry status 非 0/1/2
- 非 PARQUET 文件格式；Avro/Parquet 读取失败
- Parquet 出现 schema 之外的 field_id、缺少所需 field_id、field_id 类型不符
- equality delete 未声明 `equality_ids`、重复 ID、或引用 schema 中不存在的字段 ID
- 位置删除文件缺少 `file_path`/`pos` 列，或值非法
- URI 未被 `uri_mapping` 覆盖、映射或本地路径逃出允许根目录
- plan 中列出但 metadata 不存在的快照；ADDED 之外的 entry 序号为 null
- 同一快照内同一数据文件重复作为有效文件出现

不做写事务、schema 演进、压缩整理、GC、catalog、S3 访问或大型数据湖服务。

## 6. 测试

```sh
.venv/bin/python -m unittest discover -s tests -v
```

`tests/test_offline.py` 包含：

- 对三个快照的期望可见行断言；
- 一个**不调用扫描器模块**的独立重放 oracle：仅用 `json`/`fastavro`/`pyarrow`
  按规范重读 metadata、manifest 和 Parquet，逐行重放 position/equality 删除
  资格，比较扫描行、可见行、删除行的多重集合、读取文件集合与每条删除证据；
- 测试期生成的合成表：同一物理行重复删除证据、position 与 equality 命中同一行、
  `data_sequence == delete_sequence` 的 equality 必须保留、DELETED 文件与未引用
  文件不读取、同一数据文件在不同快照可见性不同；
- 不支持结构诊断、URI 越界/逃逸拒绝；
- 输出可独立读取、保留原文件与物理行号，且输入文件在运行前后 SHA-256 不变。
