# structedit — XLSX 结构编辑与引用迁移工具

对真实 XLSX 执行**结构编辑**（插入/删除连续行列），迁移数据与公式单元，
并扫描全工作簿重写受影响的引用与命名范围，使它们继续指向原来保留下来的单元。
不计算公式值；源文件只读。

## 运行

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 编辑：产出结果副本 + 逐公式/命名范围迁移报告
.venv/bin/python -m structedit edit \
    --source fixtures/model.xlsx \
    --operations fixtures/operations.json \
    --output out/edited.xlsx \
    --report out/report.json

# 验证：重新读取实际结果 XLSX 独立核对（退出码 0 通过 / 1 失败 / 2 拒绝）
.venv/bin/python -m structedit verify \
    --source fixtures/model.xlsx \
    --operations fixtures/operations.json \
    --result out/edited.xlsx \
    --report out/report.json

# 边界语义与拒绝路径的冒烟测试
.venv/bin/python tests/smoke_test.py
```

## 操作语义（operations.json 按数组顺序作用于当前坐标）

- `insert_rows/insert_cols`：`before` 之前插入 `count` 行/列；
- `delete_rows/delete_cols`：删除 `start` 起连续 `count` 行/列；
- 每次操作后的状态作为下一次操作的输入，不套用到初始坐标。

引用迁移（结构编辑语义，逐格跟踪被引用单元的去向，**不是**给相对引用
统一加上公式自身搬移量）：

| 情形 | 单格 | 区间 [lo,hi] |
| --- | --- | --- |
| 插入 p | p ≤ 坐标则后移 | p ≤ lo 整体移位；lo < p ≤ hi 扩展；p > hi 不变 |
| 删除 [s,e] | 落入则 `#REF!`（不改指相邻格）；其后前移 | 收缩为幸存格外接矩形；全删则 `#REF!`；收缩为单格时写单格形式 |

- `$` 绝对/混合标记原样保留，但坐标仍随结构编辑变化；
- 未限定引用绑定公式所在表；跨表引用按解析后的表名匹配；
- 带空格（`'Report Sheet'`）与撇号转义（`'O''Brien'`）的表名正确解析，前缀拼写原样保留；
- 字符串常量（如 `"Data!B4"`）、数字、函数名、名称、运算符逐字保留；
- 工作簿级与 sheet 局部简单命名范围随目标移动，公式中的名称拼写与作用域不变；
- 单元搬移保留普通样式（含行高/列宽记录随动）与工作表顺序；
- 结果中公式缓存值全部移除，并设置 `calcPr fullCalcOnLoad=1`：未求值，由打开方重算。

**拒绝本次编辑**（定位并报错，退出码 2）：外部工作簿引用、结构化表引用、
3D 引用、`INDIRECT`/`OFFSET` 动态地址、整行/整列等非 A1 单格/矩形形式。

## 交付物

- `out/edited.xlsx` — 结果副本；
- `out/report.json` — 源/输出摘要、逐操作统计、每个公式的旧/新文本与逐步
  迁移事件（含原因）、每个命名范围的旧/新定义、重算状态说明；
- `structedit/` — 工具实现（`refs` 坐标变换、`formula` 词法与重写、
  `engine` 单元搬移与报告、`identity` 身份格图、`verify` 验证器）；
- `tests/smoke_test.py` — 边界语义与拒绝路径测试。

## verify 的独立性

验证器不复用编辑器的区间代数：它以源坐标为身份建立身份格图，逐操作做
**逐点**映射，然后重新读取结果 XLSX：

1. 源摘要不变、工作表顺序不变；
2. 每个公式的新位置 = 身份格图中该公式格的去向（集合级一致）；
3. 逐引用核对：单格精确命中身份新坐标；范围双向包含（幸存的原引用格都在
   新范围内，新范围内带身份的格原来都被引用）；被删引用为 `#REF!`；
   `$` 标记与工作表前缀拼写保留；非引用 token 逐字不变；
4. 非公式格内容逐格一致（插入格为空、不多不少）；普通样式随格保留；
5. 命名范围定义迁移正确、作用域不变；
6. 用 lxml 直接解析结果 XML：`fullCalcOnLoad=1`、公式单元无 `<v>` 缓存、
   原始 `<f>` 文本与 `definedName` 文本和 openpyxl 视图一致；
7. 与 edit 报告交叉核对一致。

##  fixture 四次操作的净效果（已经 verify 通过）

- Data：第 4 行前插 2 行 → 删 7–8 行（原 R4/R5）→ 第 3 列前插 1 列 → 删第 2 列（qty）；
- `Data!D2` 等 amount 公式 → `=#REF!*C2` 等（qty 列被删，price 落回 C 列）；
- `Report Sheet!A2`、`A4` 与名称 `Amounts` 拼写不变但指向已迁移的幸存格；
- `LocalQty` → `'Data'!#REF!`；`O'Brien!A2` → `=Data!#REF!`，
  `A4` → `=SUM(Data!#REF!)`；`A3` 拼写不变（`C$7` 经四次操作回到原坐标，
  指向 R6 的 price）。
