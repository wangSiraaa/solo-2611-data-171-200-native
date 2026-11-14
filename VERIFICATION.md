# 交互验证记录

验证环境：Linux 6.8、Python 3.11.2、`TERM=xterm-256color`、`LANG=C.UTF-8`，仅用标准库。
所有端到端用例都通过 **真实 PTY**（`pty.openpty()` + `subprocess` fork 出 curses 子进程）
驱动，测试侧分块向 PTY 写入 bracketed paste 与按键字节，并用内置极简终端模拟器回读画面，
再独立读取 F2 导出的 JSON 核对值与类型。

运行方式：

```sh
python3 -m unittest discover -s tests -v
```

最近一次结果：**Ran 35 tests … OK**（完整日志见 `artifacts/test_run.log`）。

## 测试矩阵

### A. 纯逻辑单元测试（`tests/test_core.py`，25 个）

| 组 | 覆盖点 |
| --- | --- |
| TsvTests (6) | 引号内换行/制表符/`""` 转义；末尾换行不产生空记录；连续换行产生空记录；ragged 矩形；未闭合引号定位与内容还原；裸引号/闭合后多余字符；CRLF（记录外分行、引号内为数据） |
| ValueTests (7) | 空串 vs `\N` vs 整数 0 vs 布尔 false 互不混淆；非文本空字段非法；null 标记精确匹配与 nullable 约束；整数严格；十进制两位小数规范化且不用 float；布尔仅小写 true/false；日期真实日历（含闰年、02-30） |
| InputParserTests (6) | 粘贴整体单事件且内部换行不产生 Enter；UTF-8 与标记逐字节切碎仍重组；标记在每个切点拆分；方向键/Enter/Ctrl+Z；悬空 ESC 经 idle 判定与 SS3 方向键；F2 与非法 UTF-8 拒绝 |
| ModelTests (6) | 合法预演→提交→整块撤销（含多行文本）；invalid 四条类型错误定位且拒绝提交；失败预演不污染撤销栈；ragged/unclosed 预演不落表；纵向/横向越界；导出保留类型与 row_id |

### B. 真实 PTY 端到端（`tests/test_pty.py`，10 个）

| 用例 | 验证内容 |
| --- | --- |
| test_01_valid_paste_commit_undo | 3 字节分块发送（切开多字节 UTF-8 与换行）；画面可见预演/锚点/矩形/当前位置；Enter 提交 12 格；F2 导出独立核对；Ctrl+Z 整块撤销后导出与原表完全相等 |
| test_02_invalid_paste_blocks_commit | 4 条类型错误逐条显示 `源[2,c] -> R19/列(坐标): 原因`；Enter 被拒；导出与原表逐格相等（无半批写入）；无撤销记录 |
| test_03_escape_cancels_preview | 悬空 ESC 取消预演；导出证明零改动 |
| test_04_ragged_and_unclosed | 缺列与未闭合引号分别在预演定位、拒绝提交、可取消；最终导出等于原表 |
| test_05_marker_split_at_byte_boundaries | 整个粘贴流按 **1 字节**切块仍识别为单个事务并出现“可提交” |
| test_06_arrow_moves | 方向键移动、逻辑坐标与当前值实时显示；连按越过边界不崩溃、夹取到 (20,8) |
| test_07_quit | q 正常退出，返回码 0 |
| test_08_undo_stack_isolation | 成功提交后再经历“失败预演 + 取消预演”，一次 Ctrl+Z 仍恢复到最初原表；再按提示无撤销记录（仅一层） |
| test_09_row_id_not_editable | 锚点在最左 code 列粘贴，code 被改但 row_id 恒为 R01，全部 row_id 原样 |
| test_small_then_grow | 40×8 小屏显示尺寸提示且按键不崩；放大到 140×40 后界面恢复、光标 (2,2) 与数据保留、导出等于原表 |

## 关键证据

### 1. 错误预演画面（真实 PTY 回读，`invalid.tsv`）

```
预演批次: 锚点 row_id=R18 列id=name | 逻辑矩形 2行×6列 | 目标 行18-19 列2-7 | 拒绝提交
错误 4 条（源[逻辑行,列] -> 目标: 原因）:
  源[2,2] -> R19/qty(19,3): 整数格式非法：'false'（需为十进制整数，如 0、-7）
  源[2,3] -> R19/price(19,4): 十进制数需为定点文本，最多两位小数，禁止指数或浮点写法
  源[2,4] -> R19/day(19,5): 日期不存在于真实日历：'2026-02-30'
  源[2,5] -> R19/active(19,6): 布尔值仅接受小写 true 或 false，收到 'yes'
```

### 2. 合法预演画面（`valid.tsv`）

```
预演批次: 锚点 row_id=R18 列id=name | 逻辑矩形 2行×6列 | 目标 行18-19 列2-7 | 可提交
全部 12 个目标格类型校验通过，未触碰原表
```

### 3. 提交后 F2 导出的独立核对（`artifacts/verification_export.json`）

由测试进程之外的独立 Python 读取导出文件，逐项核对值与 JSON 类型，全部 OK：

```
[OK] R18.name 多行文本: value='Name 名称\nnewline' type=str
[OK] R18.qty 整数0: value=0 type=int
[OK] R18.price 两位小数字符串: value='0.00' type=str
[OK] R18.day 日期字符串: value='2026-11-14' type=str
[OK] R18.active 布尔false: value=False type=bool
[OK] R18.note 含字面制表符: value='quote "and" tab\there' type=str
[OK] R19.name: value='Second' type=str
[OK] R19.qty: value=12 type=int
[OK] R19.price: value='3.40' type=str
[OK] R19.active 布尔true: value=True type=bool
[OK] R19.note \N -> null: value=None type=NoneType
row_id 全部保留: True
未改动行 R01 与原表一致: True
未改动行 R17 与原表一致: True
ALL CHECKS PASS
```

### 4. 无半批写入

test_02 / test_03 / test_04 在被拒绝或取消的预演之后按 F2，导出 JSON 与
`fixtures/grid.json` 做整体 `assertEqual` 均相等，证明预演阶段没有任何单元格被改写。

## 实现过程中发现并修正的真实缺陷（留档）

1. 预览错误对象字段名不一致导致类型错误无法登记 —— 已统一为 `target_row_id/target_col_id`。
2. TSV 解析器曾把裸 `\r` 误当记录分隔 —— 改为仅 `\r\n` 分行，裸 `\r` 与引号内 `\r` 都是数据。
3. 输入状态机：完整但未知名的转义序列只消费 1 字节、SS3 键位表误写为四字节、悬空 ESC 的
   粘贴标记前缀判断过宽 —— 均已修正，保证方向键/ESC/拆分标记正确识别。
4. `q` 经解析器交付为 `char` 事件而 app 只匹配 `name=="q"`，导致无法退出 —— 已修正。
5. 终端缩放时 ncurses 的 `update_lines_cols()` 不刷新窗口缓存 —— 改为每帧直接
   `ioctl(TIOCGWINSZ)` 读内核真实尺寸并 `resizeterm`，小屏/放大均不错位、不丢状态。

## 复现

```sh
# 交互运行
python3 -m tuigrid --grid fixtures/grid.json --schema fixtures/schema.json \
  --export artifacts/manual.json

# 自动化验证（真实 PTY）
python3 -m unittest discover -s tests -v
```
