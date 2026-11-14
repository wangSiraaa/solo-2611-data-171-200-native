# nbexec — 本地 Notebook 执行器

基于 `jupyter_client` + `nbformat` 直接实现的本地执行器（Python 3.11），**不封装
nbclient 的整本 `execute`**：每个 execute 请求、每条通道消息、每次输出变更都在本仓库
代码中处理，消息归属、显示语义和执行记录完全可检查。不涉及任何远程 Notebook 平台。

## 目录结构

```
nbexec/                  执行器源码包
  cli.py                 run / inspect 两个子命令
  runner.py              执行引擎：消息泵、parent header 归属、超时/中断、结构化记录
  kernel.py              内核生命周期：启动、有限等待关闭、进程回收（可重复调用）
  outputs.py             输出状态机：stream/display/execute_result/update/clear 语义
  parameters.py          parameters 标签 cell 的安全改写（只允许覆盖已有标量名）
  inspect.py             执行记录与输出 notebook 的可读渲染
fixtures/                题面输入（只读；另含本项目补充的 stdin.ipynb 边界用例）
tests/                   不依赖内核的状态机/参数单元测试
scripts/verify.py        独立验收脚本：重新打开产物并与消息记录交叉核对
run_all.sh               每份 notebook 独立启动一个新内核依次执行
results/                 输出副本（.ipynb）与结构化执行记录（.record.json）
```

## 安装

```sh
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install pytest        # 仅运行 tests/ 时需要
```

## 执行（每份 notebook 一个全新内核）

```sh
./run_all.sh
```

等价的单条命令：

```sh
.venv/bin/python -m nbexec run \
  --notebook fixtures/analysis.ipynb \
  --output   results/analysis.ipynb \
  --record   results/analysis.record.json \
  --parameters fixtures/parameters.json \
  --plan        fixtures/plan.json \
  --working-directory "$PWD"
```

关键参数：

| 参数 | 含义 |
| --- | --- |
| `--cell-timeout` | 单 cell 超时秒数；缺省读取 plan 的 `cell_timeout_seconds`，再缺省 30s |
| `--interrupt-wait` | 超时后中断的有限宽限窗口，以及关闭内核时软/硬等待上限 |
| `--working-directory` | 内核 cwd；fixture 用相对路径读 `fixtures/input.csv`，须传仓库根 |
| `--parameters` | JSON 标量覆盖文件，只允许覆盖 parameters cell 中已存在的名字 |

每次 `run` 都新建并最终回收一个 python3 内核进程；多次运行/多份 notebook 之间互不共享内核。

## 查看记录与产物

```sh
.venv/bin/python -m nbexec inspect --record results/analysis.record.json
.venv/bin/python -m nbexec inspect --record results/timeout.record.json --messages
.venv/bin/python -m nbexec inspect --notebook results/failure.ipynb
```

## 验收

```sh
.venv/bin/python scripts/verify.py     # 基于 results/ 的端到端断言（退出码 0 即通过）
.venv/bin/python -m pytest tests/ -q   # 状态机与参数改写单元测试
```

## 设计要点（与需求逐条对应）

1. **新内核与文档顺序**：每次 run 用 `KernelManager(kernel_name="python3")` 启动新内核，
   `wait_for_ready` 后按文档顺序只执行 code cell；markdown cell 原样保留。
2. **参数覆盖**：用 AST 解析 parameters cell，仅接受 `name = JSON标量字面量`；覆盖文件只能替换
   已存在的名字（未知名字/非标量直接报错，退出码 2），不向内核注入任意代码。
3. **旧输出清理**：先在内存副本上清空全部 code cell 的 `outputs` 与 `execution_count`，
   源文件从不写回；未执行的后续 cell 保持空输出，不可能展示上次的成功结果。
4. **真实消息关联**：`kc.execute(..., reply=False)` 拿到请求 `msg_id`；shell 的
   `execute_reply` 与 IOPub/stdin 消息都按 `parent_header.msg_id == 请求id` 判定归属，
   不匹配的消息（如启动期 idle）记录为 `accepted=false` 但不参与输出。cell 必须**同时**
   收到本请求的 shell reply 与 IOPub `status: idle` 才算完成，不会只等到其中一个就结束。
5. **显示语义**（`nbexec/outputs.py`）：
   - `stream` 按到达顺序保存，相邻同名 stream 自动合并；
   - `display_data` / `execute_result` 完整保留 MIME bundle 与 execution_count；
   - `update_display_data` 按 `display_id` 更新**本轮内核运行中**所有匹配位置，包括更早
     cell 的输出（`display_id` 按 nbformat schema 存于 `metadata.transient`）；
   - `clear_output(wait=True)` 只置“延迟清除”标记，下一条输出到来时才清空；指令本身不会
     成为普通输出；cell 结束前没有新输出则标记丢弃，保留最后可见结果。
6. **错误/超时停止**：首个错误或超时即停止调度后续 cell，已产生的有效输出与错误依据
   （traceback / KeyboardInterrupt）保留。超时流程：到点发中断 → 有限宽限窗口等 reply+idle
   → 窗口内恢复则记录 timeout 终态 → 仍无响应则强制关闭并补一个 TimeoutError 输出。
7. **stdin**：execute 固定 `allow_stdin=False`；同时监听 stdin 通道，任何 `input_request`
   都会显式令 cell 失败并中断，绝不永久等待（ipykernel 7 自身也会直接抛
   `StdinNotImplementedError`，两条路径都被覆盖）。
8. **进程回收**：关闭走“停止通道 → 优雅 shutdown（有限等待）→ 强制 kill（有限等待）”，
   并以 `os.kill(pid, 0)` 确认进程消失；`shutdown()` 幂等，runner 用 `try/finally` 保证
   异常路径下也回收。
9. **结构化记录**：每个 run 产出 `*.record.json`，包含 run 元数据、参数变更、内核 pid/版本/
   关闭报告、逐 cell 终态（含 `request_msg_id`、`reply_msg_id`、execution_count、输出数、
   消息序号范围、更新/中断备注）以及**全量消息日志**（通道、类型、请求 id、parent id、
   是否归属本请求、摘要）。可从任一输出 cell 经 `request_msg_id` 追到产生它的消息序列。

## 不支持的范围

Widgets/comm 交互、交互式 stdin、并行内核、DAG 缓存、远程 Notebook 平台均不在范围内；
comm 消息只记录不渲染。
