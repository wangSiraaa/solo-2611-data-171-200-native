# 题目输入材料

fixtures/performance.mid 是三轨真实SMF格式1文件；plain.mid是恒速小文件。rules.json明确同tick顺序、同音配对、踏板及截断口径。

安装 `python -m pip install -r requirements.txt`。MIDI只描述控制事件，本题实际发声指遵守声明的踏板生命周期，不模拟乐器包络自然衰减。

所有数据均为合成材料。实现代码、结果与独立核验由作答者完成。输入中的数字和规则是业务定义，不是参考答案。

---

# SMF 格式 1 音符生命周期解析工具（实现说明）

离线解析 SMF 格式 1，输出每个音符的 **onset（按下）/ key_release（松键）
/ sound_end（实际结束发声）** 三个时间、踏板控制事件证据与本地 HTML 时间线。
只用 mido 解码，不合成音频；“发声”严格按 CC64/CC66 声明的踏板生命周期推导。

## 环境与运行

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt   # mido==1.3.3

# convert：产出 JSON / 两个 CSV / 本地 HTML（默认 outputs/）
.venv/bin/python -m smf_lifecycles convert fixtures/performance.mid -o outputs --prefix performance
.venv/bin/python -m smf_lifecycles convert fixtures/plain.mid
.venv/bin/python -m smf_lifecycles convert examples/tempo_conflict_demo.mid

# verify：重新解析并核验不变量（可选 --expected 对比期望 JSON）
.venv/bin/python -m smf_lifecycles verify fixtures/performance.mid

# 全部单元测试（25 个）
bash run_tests.sh
```

## 处理管线（关键口径）

1. **delta→绝对 tick 与统一排序**（`reader.py`）：各轨 delta tick 累加为
   整数绝对 tick，全轨事件按唯一键 `(absolute_tick, track_index, event_index)`
   排序——同 tick 顺序即规则文件规定的先轨索引再事件索引，不依赖读取偶然顺序。
   每个事件保留来源轨/事件索引；`midi_port` 是轨本地 meta，只影响该轨后续
   事件，端口默认 0。
2. **全局分段 tempo 映射**（`timebase.py`）：所有轨的 tempo 事件统一建表，
   绝不按轨分别换秒再拼接。同 tick 不同 tempo 产生 `tempo_conflict` 诊断
   （列出候选与获胜者），按规范顺序首个事件生效；同值重复折叠。秒由整数
   tick 跨度以 `Fraction` 分段积分后转 float64，tempo 边界严格连续。
   JSON 保留完整 float64；CSV/HTML 四舍五入到微秒（1e-6 s）展示，
   精度说明写入 `seconds_precision` 字段。整数 tick 始终是权威轴。
3. **声部状态机**（`engine.py`）：状态按 `(port, channel)` 共享。
   - note_on(v>0) 产生新实例进入同音高 FIFO 队尾；note_off 与零力度
     note_on 等价为松键，按 FIFO 配给“尚按下”的最旧实例；已松键仍响者
     不再参与配对，重触发产生新实例不覆盖旧音。无匹配松键仅诊断不伪造。
   - CC64 延音：踩下保持当时在响实例（含已松键者）；抬起只止音已松键且
     无其他保持的实例。
   - CC66 保持踏板：仅在下阈值→上阈值跳变瞬间捕获当时仍按键的实例，
     重复踩不重新捕获，之后按下的音不自动锁定；抬起释放捕获，延音可继续保持。
   - 两踏板可叠加；文件结束仍在响的实例在全局最大 EOT tick 截断，显式
     `truncated=true` 与 `eof_truncated` 原因，不伪称正常释放。
   - 零长度音符保留完整事件证据；单轨 EOT 不影响其他轨共享声道的在响音。

## 目录

```
smf_lifecycles/   reader/timebase/engine/model/analyze/verify/cli
tests/            25 个 unittest（fixture 端到端、手工状态表、踏板叠加、tempo 连续性、CLI 产物）
tools/            生成 tempo 冲突演示 MIDI
examples/         tempo_conflict_demo.mid
outputs/          三个输入各自的 *_analysis.json / *_notes.csv /
                  *_diagnostics.csv / *_timeline.html
VERIFICATION.md   手工状态表与独立核验记录
```

范围：三轨、少量端口/声道、note_on/off 与 CC64/CC66；复杂 SysEx、MPE、
乐器音色自然衰减不在范围内。

