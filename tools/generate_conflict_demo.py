"""生成演示用 SMF：同 tick 跨轨 tempo 冲突 + 同值重复 tempo。

输出 examples/tempo_conflict_demo.mid：
  tick 240：track0 set_tempo=400000 与 track1 set_tempo=600000 冲突
            （按同 tick 顺序 track0 生效，冲突进诊断）
  tick 480：两轨都 set_tempo=500000（同值重复，折叠不诊断）
另含一个跨 (port, channel) 的短音符，便于观察秒轴。
"""

from pathlib import Path

import mido

TPB = 480


def main() -> None:
    mid = mido.MidiFile(type=1, ticks_per_beat=TPB)

    t0 = mido.MidiTrack()
    t0.append(mido.MetaMessage("set_tempo", tempo=500000, time=0))
    t0.append(mido.MetaMessage("set_tempo", tempo=400000, time=240))
    t0.append(mido.MetaMessage("set_tempo", tempo=500000, time=240))
    t0.append(mido.MetaMessage("end_of_track", time=480))

    t1 = mido.MidiTrack()
    t1.append(mido.MetaMessage("midi_port", port=0, time=0))
    t1.append(mido.Message("note_on", channel=0, note=60, velocity=80, time=0))
    t1.append(mido.MetaMessage("set_tempo", tempo=600000, time=240))  # 与 t0 冲突
    t1.append(mido.MetaMessage("set_tempo", tempo=500000, time=240))  # 同值重复
    t1.append(mido.Message("note_off", channel=0, note=60, velocity=0, time=240))
    t1.append(mido.MetaMessage("end_of_track", time=480))

    mid.tracks = [t0, t1]
    out = Path(__file__).resolve().parents[1] / "examples" / "tempo_conflict_demo.mid"
    out.parent.mkdir(parents=True, exist_ok=True)
    mid.save(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
