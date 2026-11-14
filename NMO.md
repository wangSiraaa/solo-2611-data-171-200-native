# NMO 校正与叠加 — 运行说明

按 `README.md` 的固定物理约定实现（教材级 NMO，不做速度反演、拾取、
去噪或地层解释）。Python 3.11 + NumPy + segyio。

## 运行

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m nmo run fixtures/gathers.sgy fixtures/velocity.csv fixtures/plan.json \
    --out nmo_output --explain 101 7 180
```

解释单个输出样本（源索引、插值权重、速度、切除判断）：

```sh
.venv/bin/python -m nmo explain fixtures/gathers.sgy fixtures/velocity.csv fixtures/plan.json \
    --cdp 101 --trace 7 --t0-index 180        # --trace 是文件顺序号 file_index
```

验证：` .venv/bin/python -m unittest discover tests -v `（30 个测试）。

## 处理链

1. **读取**（`nmo/segy_reader.py`）：segyio 以 `ignore_geometry` 打开；
   校验格式码（仅接受 1/5 四字节浮点）、二进制头与每道头的样本数/
   采样间隔一致性（道头为 0 视为未写并回落到二进制头，非零不一致即
   `HeaderContradictionError`）。CDP(21–24)、带符号 offset(37–40，米)、
   TraceIdentificationCode(29–30)、DelayRecordingTime(109–110，毫秒)、
   样本数(115–116)、间隔(117–118，微秒)均按真实道头读取并换算成秒。
   仅 `code == plan.live_trace_code` 为 live，其余整道记 `DEAD_TRACE`
   排除并报告。文件顺序不作为任何排列依据，按真实 CDP 归组。
2. **速度**（`nmo/velocity.py`）：给定 RMS 表，每 CDP 节点须严格递增、
   速度为正；在输出 t0 上线性插值，不覆盖输出网格即报错，不外推。
3. **校正**（`nmo/core.py`）：`tx = sqrt(t0² + (offset/v)²)`（负 offset
   映射相同，符号与道身份保留）；检查每道 tx 在计划网格上严格递增，
   折回即 `NonMonotonicMappingError`。道的真实采样时刻为
   `delay_s + k*dt_s`（样本索引不当作共同零时刻）；仅在记录支持
   `[delay, delay+(ns-1)*dt]` 内线性插值，精确命中首末样本允许，
   不外推。伸长 `tx/t0 − 1`（t0=0 时 offset=0 取 0、否则无穷），
   严格大于 `stretch_limit` 切除，等于保留。不做振幅 Jacobian 补偿。
4. **叠加**：fold 只计当时有效道；`stack = sum(valid)/fold`，fold=0
   为缺失（NaN/空），缺失从不填零。原因码：`OK / DEAD_TRACE /
   STRETCH_MUTE / OUT_OF_SUPPORT`，逐样本存于 reason 矩阵。

## 输出（`nmo_output/`）

- `nmo_result.npz`：每 CDP 的校正矩阵（无效为 NaN）、valid mask、
  reason、tx、stretch、v(t0)、fold、stack_sum、stack 及道身份数组。
- `stacks.csv`：长表 `cdp,t0_s,fold,stack_sum,stack_mean`，fold=0 时
  均值为空。
- `traces.csv`：道登记（CDP、file_index、tracl、offset、delay、live、
  各原因样本计数）。
- `corrected_cdp*.csv` / `*_reason.csv`：每 CDP 校正矩阵与原因矩阵，
  无效单元为空。
- `run_meta.json`：计划参数、排除道清单、原因码表。

## 验证要点（tests/test_nmo.py）

- 真实材料：非零 delay（20 ms 道源采样轴平移 10 个样本）、±300 m
  映射一致且符号/身份保留、dead 道（file_index 5）排除且不计 fold、
  900 m 道伸长切除前缀与超出支持后缀（23 样本）逐项核对。
- 合成线性振幅道：手算插值（如 t0=0.02 → tx=0.025 → r=4.25，
  0.75·s₄+0.25·s₅）与程序结果一致；零 offset 在共同支持上保持原
  时间线，delay 之前为缺失而非零。
- fold/sum/stack 由独立逐样本双重循环累加 mask 复算核对；无效样本
  为 NaN 且绝不填零；fold=0 时 stack 为缺失。
- 诊断：格式码、道头/二进制头矛盾、速度折回/非正/覆盖不足、
  非单调映射、非法 plan 均抛明确异常，CLI 以退出码 2 报告。
