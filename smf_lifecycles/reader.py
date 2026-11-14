"""SMF 解析层：mido 解码 + delta tick 转绝对 tick + 统一排序。

关键口径（fixtures/rules.json）：
- 同 tick 事件顺序为 track index 再 event index，排序键
  (absolute_tick, track_index, event_index) 全局唯一，不依赖文件读取偶然顺序。
- midi_port 是轨本地 meta 状态：影响该轨后续事件，端口默认 0。
- 只读取消息与整数 tick；秒轴换算统一在 timebase 层完成，不在此处按轨换算。
"""

from __future__ import annotations

from dataclasses import dataclass

import mido

from .model import EventKind, EventRef, MidiEvent

SUSTAIN_CC = 64
SOSTENUTO_CC = 66
PEDAL_THRESHOLD = 64


@dataclass
class ParsedFile:
    type: int
    ticks_per_beat: int
    events: list[MidiEvent]          # 已按 (tick, track, event_index) 排序
    eof_tick: int                    # 全局最大绝对 EOT tick
    track_count: int
    track_end_ticks: list[int]


def _classify(msg: mido.Message) -> EventKind:
    if msg.type == "note_on":
        return EventKind.NOTE_ON
    if msg.type == "note_off":
        return EventKind.NOTE_OFF
    if msg.type == "control_change" and msg.control == SUSTAIN_CC:
        return EventKind.SUSTAIN
    if msg.type == "control_change" and msg.control == SOSTENUTO_CC:
        return EventKind.SOSTENUTO
    if msg.type == "set_tempo":
        return EventKind.SET_TEMPO
    if msg.type == "midi_port":
        return EventKind.MIDI_PORT
    if msg.type == "end_of_track":
        return EventKind.END_OF_TRACK
    return EventKind.OTHER


def parse_file(path: str) -> ParsedFile:
    """解析一个 SMF 文件，返回全局排序后的事件流。"""
    mid = mido.MidiFile(path)
    if mid.type != 1:
        raise ValueError(f"仅支持 SMF 格式 1，收到 type={mid.type} 文件: {path}")
    if mid.ticks_per_beat <= 0:
        raise ValueError(f"非法 ticks_per_beat={mid.ticks_per_beat}")

    events: list[MidiEvent] = []
    track_end_ticks: list[int] = []

    for track_index, track in enumerate(mid.tracks):
        absolute_tick = 0
        current_port = 0  # port_default = 0
        saw_eot = False

        for event_index, msg in enumerate(track):
            absolute_tick += int(msg.time)
            kind = _classify(msg)
            ref = EventRef(track_index, event_index, absolute_tick)

            if kind is EventKind.MIDI_PORT:
                current_port = int(msg.port)
                events.append(MidiEvent(
                    ref=ref, kind=kind, port_at_event=current_port,
                    value=current_port, raw_type=msg.type,
                ))
                continue

            kwargs: dict = {"ref": ref, "kind": kind, "raw_type": msg.type}

            if hasattr(msg, "channel"):
                kwargs["channel"] = int(msg.channel)
                kwargs["port_at_event"] = current_port
            if hasattr(msg, "note"):
                kwargs["note"] = int(msg.note)
            if hasattr(msg, "velocity"):
                kwargs["velocity"] = int(msg.velocity)
            if hasattr(msg, "control"):
                kwargs["control"] = int(msg.control)
            if hasattr(msg, "value"):
                kwargs["value"] = int(msg.value)
            if kind is EventKind.SET_TEMPO:
                kwargs["tempo_us"] = int(msg.tempo)

            events.append(MidiEvent(**kwargs))

            if kind is EventKind.END_OF_TRACK:
                saw_eot = True
                track_end_ticks.append(absolute_tick)

        if not saw_eot:
            # 规范上 SMF 轨应以 EOT 结束；缺失时用该轨最后一个事件的 tick
            track_end_ticks.append(absolute_tick)

    # 全局唯一排序：同 tick 先 track index 再 event index
    events.sort(key=lambda e: (e.ref.tick, e.ref.track, e.ref.event_index))

    eot_ticks = [e.tick for e in events if e.kind is EventKind.END_OF_TRACK]
    if eot_ticks:
        eof_tick = max(eot_ticks)
    else:
        eof_tick = max((e.tick for e in events), default=0)

    return ParsedFile(
        type=mid.type,
        ticks_per_beat=mid.ticks_per_beat,
        events=events,
        eof_tick=eof_tick,
        track_count=len(mid.tracks),
        track_end_ticks=track_end_ticks,
    )
