# 独立核验说明（VERIFICATION）

核验不依赖 convert 的落盘产物自证：`smf_lifecycles/verify.py` 重新解析
MIDI、重建 tempo 映射并重跑状态机，再检查结构性不变量；`tests/` 下另有
25 个 unittest 用例，其中包含手工微型序列状态表与秒轴连续性核对。

## 1. performance.mid 手工状态表（权威整数 tick 轴）

tpb=480；tempo：tick0 起 500000 µs/beat，tick960 起 750000 µs/beat；
全局 EOT = 1920 tick。CC64/CC66 位于 track2，与 track1 共享 (port0, ch0)。

| tick | 事件（轨.事件） | 状态变化与存活实例 |
|---:|---|---|
| 0    | T1.e1 on p60 v90；T2.e0 port=0 | 实例 #1 按下 |
| 120  | T2.e1 on p62 v75 | #2 按下 |
| 240  | T1.e2 on p64 v80 | #3 按下 |
| 360  | T2.e2 **CC66=127 踩** | 捕获当时仍按键者：**#1,#2,#3**（#7/#5/#9 尚未出现，不捕获） |
| 400  | T1.e3 on p69 v70 | #4 按下 |
| 440  | T1.e4 off p69 | #4 松键且无踏板 → 440 立即止音（key_release） |
| 480  | T1.e5 off p60 | #1 松键；CC66 捕获中 → 继续响 |
| 600  | T2.e3 **CC64=127 踩** | 在响实例 #1,#2,#3 全部进入延音保持（#4 已止音） |
| 650  | T2.e4 off p62 | #2 松键；双踏板保持 → 继续响 |
| 720  | T1.e6 on p64 v0（零力度=松键） | #3 松键；继续响 |
| 800  | T1.e7 on p67 v70 | #5 按下（CC66 已踩，**不重新捕获**；CC64 踩着→受延音作用） |
| 900  | T1.e8 off p67 | #5 松键；延音保持 → 继续响 |
| 1000 | T2.e5 **CC66=0 抬** | 释放 #1/#2/#3 的保持踏板捕获；CC64 仍踩 → **无一止音** |
| 1200 | T2.e6 **CC64=0 抬** | #1,#2,#3,#5 已松键且无其他保持 → 同 tick 止音（sustain_up） |
| 1300 | T1.e9 on p60 v88 | #6 按下（踏板均已抬） |
| 1340 | T1.e10 on p60 v77；T2.e7 on ch1 p65 v80 | #7 重触发（不覆盖 #6）；#8 在 ch1 独立 |
| 1400 | T1.e11 off p60；T2.e8 off ch1 p65 | FIFO 配给最旧仍按键的 **#6** → 1400 止音；#8 → 1400 止音 |
| 1450 | T1.e12 off p60 | 配给 #7 → 1450 止音 |
| 1500 | T1.e13 on p72 v60 | #9 按下 |
| 1550 | T1.e14 off p50 | 无 p50 实例 → **unmatched_release 诊断**，不伪造音符 |
| 1920 | 全局 EOT | #9 仍在响 → 1920 **截断**（eof_truncated），显式标记非正常释放 |

工具输出与上表 9 个实例的 onset/key_release/sound_end/stop_cause 逐项一致
（见 `tests/test_fixtures.py::EXPECTED_PERF`，自动化断言）。

## 2. 秒轴连续性核对（不在轨内各自换秒）

tempo 映射由全局事件统一构建，整数 tick 跨度用
`span_seconds = span_tick × tempo_us / tpb / 1e6` 分段积分，段起点用
`Fraction` 精确累加：

- tick 960 边界：左段 960×500000/480/1e6 = **1.000000 s**；右段起点同为
  **1.000000 s**（`tempo_boundary_continuity.violations == []`）。
- 曲末 tick1920：1.0 + 960×750000/480/1e6 = 1.0 + 1.5 = **2.500000 s**。
- 抽样 tick1300：1.0 + 340×750000/480/1e6 = **1.531250 s**（与 JSON 一致）。
- `tests/test_pedals_and_tempo.py` 另对 959/960/961 三个 tick 断言两侧数值
  与手工 `Fraction` 积分相等。

## 3. 同时事件与保持踏板捕获时刻的微型核对

`tests/test_state_table.py` 用 mido 现场构造微型 SMF 并逐时刻断言：

- `test_same_tick_track_then_event_order`：同 tick 轨0先踩 CC64、轨1后按下，
  事件流严格为 `(0, cc64)` 再 `(1, note_on)`，音符因此被延音保持。
- `test_sostenuto_capture_moment_state_table`：CC66 踩下瞬间只捕获当时
  仍按键的两个同音高实例；踩下之后才出现的音永不被捕获；松键按 FIFO
  配给最旧实例，踏板抬起才止音。
- `test_zero_length_note_kept_with_evidence`：同 tick 按下/松键的零长度
  音符保留完整事件证据并出诊断。
- `test_port_meta_scopes_channel_state`：midi_port 轨本地状态使同 channel
  不同 port 的声部互不影响。
- `test_early_track_end_does_not_silence_shared_channel`：一轨在 200 tick
  EOT，另一轨共享声道的音仍存活到全局 EOT 400，不被提前静音。
- `tests/test_pedals_and_tempo.py`：两踏板叠加的两个方向（先抬延音 /
  先抬保持踏板）均验证“一个踏板抬起不一定止音”；延音中重触发验证旧实例
  不被覆盖、松键不再二次配对。

## 4. tempo 冲突诊断核对

`examples/tempo_conflict_demo.mid`：tick240 两轨给出 400000 与 600000
两个不同 tempo。工具输出 `tempo_conflict` 诊断（含全部候选与获胜者），
按同 tick 规范顺序采用 track0 的 400000，而非文件读取偶然顺序；tick480
两处同值 500000 折叠不诊断；秒轴边界仍连续。

## 5. 恒速无踏板对照

`plain.mid`（120 BPM 恒定、无任何 CC）：两个音符均在松键 tick 立即
key_release 止音，960 tick = 1.0 s，零诊断，单 tempo 段，与手工计算一致。
