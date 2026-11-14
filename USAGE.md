# TUI 小表 bracketed-paste 粘贴编辑器

基于 **Python 3.11 标准库 `curses`** 的真实终端 TUI，用于在固定 **20 行 × 8 列**小表上
整批粘贴 TSV、先预演查错、再一次提交并支持一层整块撤销。无浏览器、无网络、无数据库、
无第三方依赖。

## 目录结构

```
src/tuigrid/
  tsv.py     # TSV 解析器：双引号字段、"" 转义、字段内制表符/换行、未闭合引号定位
  values.py  # schema 类型校验：空串 / \N / 整数0 / 布尔false / 十进制字符串严格区分
  model.py   # 网格模型：预演（不落表）、整批提交、单层撤销、JSON 导出
  input.py   # 字节级输入状态机：bracketed paste、跨 read 的 UTF-8/转义标记拆分
  app.py     # curses 界面：状态栏、网格、预演面板、缩放保护、颜色高亮
  __main__.py# 命令行入口
tests/
  test_core.py # 25 个纯逻辑单元测试（TSV/类型/输入解析/模型）
  test_pty.py  # 10 个真实 PTY 端到端测试（含极简终端模拟器，断言屏幕画面与导出 JSON）
```

## 启动

```sh
# 仅编辑（不配置 F2 导出路径时，按 F2 会提示缺少路径）
python3 -m tuigrid --grid fixtures/grid.json --schema fixtures/schema.json

# 带 F2 导出目标路径（推荐）
python3 -m tuigrid \
  --grid fixtures/grid.json \
  --schema fixtures/schema.json \
  --export artifacts/export.json
```

> 需要在支持 bracketed paste 的真实终端里运行（xterm、gnome-terminal、tmux、
> iTerm2、Windows Terminal 等均可）。最小终端尺寸 110 列 × 26 行；不足时只显示
> 尺寸提示，状态与数据完整保留，放大后自动恢复，不会索引错位或崩溃。

## 按键

| 按键 | 作用 |
| --- | --- |
| 方向键 / Home / End | 移动当前格（边界自动夹取，不越界） |
| 终端粘贴 | 自动以 bracketed paste 包裹，作为**一个粘贴事务**进入预演 |
| **Enter** | 预演全部合法且矩形放得下时，整批提交；否则拒绝 |
| **Esc** | 取消当前预演，网格零改动 |
| **Ctrl+Z** | 撤销最近一次**成功提交**的粘贴，整块恢复原值与类型（仅一层，无 redo） |
| **F2** | 把当前表导出到 `--export` 指定的 JSON 路径 |
| q | 退出（预演中需先 Enter 或 Esc） |

## 粘贴与预演语义

- **锚点**在 bracketed paste 开始标记到达瞬间捕获：记录当前 `row_id` 与列 id，
  预演期间移动光标不改变该批次锚点。
- 粘贴内容里的换行（含引号字段内换行）只是数据，**绝不会被当成 Enter 确认键**；
  开始/结束标记或多字节 UTF-8 字符被拆到多次 `read()` 也能正确重组为一个批次。
- TSV 支持双引号字段、字段内 `""` 转义、字段内制表符与换行；末尾记录的换行不产生
  额外空行。缺列、未闭合引号、越过 20×8 边界都会在预演中按**逻辑源行/列 → 目标坐标**
  逐条定位。
- 类型严格按 schema 区分，不使用 Python 真假值或 `float` 隐式转换：
  - 文本空字段 = 空字符串 `""`；非文本空字段非法；
  - 空值只能写精确的 `\N` 且该列 `nullable`；
  - 整数 `0` 是 `int`，布尔 `false` 是 `False`，二者都不等于空/None；
  - 十进制是定点文本，最多两位小数，规范化为两位小数字符串（如 `2.0`→`2.00`），
    禁止指数与浮点写法；日期做真实日历校验。
- **预演不修改原表**；只有 Enter 且 `ok` 时才提交，提交前深拷贝整块快照供撤销。
  失败或取消的预演不会覆盖撤销记录。
- `row_id` 不在可粘贴列内，任何粘贴都无法改写它；导出时原样保留。

## 导出

F2 导出与 `fixtures/grid.json` 同构的 JSON（`[{"row_id": ..., "cells": [...]}, ...]`），
保留每个单元格的 JSON 类型（str/int/bool/null）与全部 row_id，可独立核对。

## 运行测试

```sh
# 纯逻辑单元测试
python3 -m unittest tests.test_core -v

# 真实 PTY 端到端测试（会真实 fork 子进程、开 PTY、分块发字节）
python3 -m unittest tests.test_pty -v

# 全部
python3 -m unittest discover -s tests -v
```

PTY 测试内置一个极简终端模拟器（支持光标定位、SGR、东亚宽字符宽度、擦除），
既断言屏幕上能看到预演/错误/当前位置，也独立读取导出 JSON 核对值与类型，并验证
错误预演不存在任何半批写入。验证过程见 [VERIFICATION.md](VERIFICATION.md)。

## 明确不做

公式、筛选、排序、HTML 剪贴板、虚拟滚动、redo、并发编辑、网络/数据库校验。
