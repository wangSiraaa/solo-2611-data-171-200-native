"""测试辅助：用 mido 在内存中构造 SMF 格式 1 文件。

track_specs: 每个轨是 [(absolute_tick, mido.Message/MetaMessage), ...]，
内部自动转 delta tick 并追加 end_of_track。
"""

from __future__ import annotations

import mido

TPB = 480


def build_mid(track_specs, tpb: int = TPB, eot_tick: int | None = None) -> mido.MidiFile:
    mid = mido.MidiFile(type=1, ticks_per_beat=tpb)
    global_end = eot_tick
    if global_end is None:
        global_end = max((m[0] for spec in track_specs for m in spec), default=0)

    for spec in track_specs:
        track = mido.MidiTrack()
        last = 0
        for abs_tick, msg in sorted(spec, key=lambda x: x[0]):
            msg.time = abs_tick - last
            last = abs_tick
            track.append(msg)
        eot = mido.MetaMessage("end_of_track", time=max(0, global_end - last))
        track.append(eot)
        mid.tracks.append(track)
    return mid


def save_mid(mid: mido.MidiFile, path: str) -> str:
    mid.save(path)
    return path


def analyze_bytes(track_specs, tpb: int = TPB, eot_tick: int | None = None):
    """构造 → 落临时文件 → 走完整 analyze 管线。"""
    from pathlib import Path
    import tempfile
    from smf_lifecycles.analyze import analyze

    mid = build_mid(track_specs, tpb, eot_tick)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "case.mid"
        mid.save(p)
        return analyze(str(p))


def note_on(ch, note, vel=80):
    return mido.Message("note_on", channel=ch, note=note, velocity=vel, time=0)


def note_off(ch, note):
    return mido.Message("note_off", channel=ch, note=note, velocity=0, time=0)


def cc(ch, control, value):
    return mido.Message("control_change", channel=ch, control=control,
                        value=value, time=0)


def tempo(us):
    return mido.MetaMessage("set_tempo", tempo=us, time=0)


def port(p):
    return mido.MetaMessage("midi_port", port=p, time=0)
