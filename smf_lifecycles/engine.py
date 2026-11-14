"""音符/踏板状态机：推导 onset、key_release、sound_end。

状态口径（fixtures/rules.json）：
- 声部状态按 (port, channel) 共享；midi_port meta 只影响所在轨后续事件。
- note_on（velocity>0）产生新实例并进入该 (port, channel, pitch) 的 FIFO 队尾；
  零力度 note_on 与 note_off 等价，均为松键。
- 松键按 FIFO 配给“尚按下”的最旧实例；已松键但仍在响的实例不再参与配对。
  无匹配松键仅诊断，不伪造音符。
- CC64 延音：踏下时该 (port, channel) 全部仍在响的实例（含已松键者）
  被保持；抬起时停止所有“已松键且未被保持踏板捕获”的实例。
- CC66 保持踏板：仅在 <64 → >=64 的跳变瞬间捕获“当时仍按键”的实例；
  重复 down 不重新捕获；抬起释放捕获（延音可能继续保持）。
- 两踏板可叠加：一个抬起不一定止音。
- 文件结束仍在响的实例在全局 EOT tick 截断并显式标记，不伪称正常释放。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .model import (
    Diagnostic,
    DiagCode,
    EventKind,
    NoteInstance,
    PedalEvent,
    StopCause,
)
from .reader import PEDAL_THRESHOLD
from .timebase import TempoMap

Key = tuple[int, int]          # (port, channel)
PitchKey = tuple[int, int, int]  # (port, channel, pitch)


@dataclass
class GroupState:
    """一个 (port, channel) 的踏板与全部在响实例。"""

    sustain_down: bool = False
    sostenuto_down: bool = False
    # 每个音高一条 FIFO：只包含尚未止音的实例；按下入队尾，止音后移除
    fifo: dict[int, "deque[NoteInstance]"] = field(default_factory=dict)

    def all_instances(self) -> list[NoteInstance]:
        out: list[NoteInstance] = []
        for q in self.fifo.values():
            out.extend(q)
        return out


class Engine:
    def __init__(self, tempo_map: TempoMap):
        self.tempo_map = tempo_map
        self.groups: dict[Key, GroupState] = {}
        self.notes: list[NoteInstance] = []
        self.pedal_events: list[PedalEvent] = []
        self.diagnostics: list[Diagnostic] = []
        self._next_id = 1

    # ---- 内部工具 ----

    def _group(self, key: Key) -> GroupState:
        return self.groups.setdefault(key, GroupState())

    def _seconds(self, tick: int) -> float:
        return self.tempo_map.tick_to_seconds(tick)

    def _stop(self, note: NoteInstance, tick: int, ref,
              cause: StopCause) -> None:
        note.sounding = False
        note.sound_end_tick = tick
        note.sound_end_seconds = self._seconds(tick)
        note.sound_end_event = ref
        note.stop_cause = cause
        note.truncated = cause is StopCause.EOF_TRUNCATED
        q = self.groups[(note.port, note.channel)].fifo.get(note.pitch)
        if q is not None:
            try:
                q.remove(note)
            except ValueError:
                pass
            if not q:
                del self.groups[(note.port, note.channel)].fifo[note.pitch]

    def _maybe_stop_on_release(self, note: NoteInstance, tick: int, ref) -> None:
        """松键后若无任何踏板保持则立即止音。"""
        if not note.key_down and note.sounding and not note.protected():
            self._stop(note, tick, ref, StopCause.KEY_RELEASE)

    # ---- 事件处理 ----

    def handle_note_on(self, ev) -> None:
        key = (ev.port_at_event, ev.channel)
        if ev.velocity == 0:
            # 零力度 note_on：等价松键
            self._handle_release(ev, key)
            return

        gs = self._group(key)
        note = NoteInstance(
            note_id=self._next_id,
            port=ev.port_at_event,
            channel=ev.channel,
            pitch=ev.note,
            onset_tick=ev.tick,
            onset_seconds=self._seconds(ev.tick),
            velocity=ev.velocity,
            sustain_held=gs.sustain_down,          # 延音踩着时新音受其作用
            sostenuto_captured=False,              # 后来按下的音不被保持踏板自动锁住
            onset_event=ev.ref,
        )
        if gs.sustain_down:
            note.sustain_held_ever = True
        self._next_id += 1
        self.notes.append(note)
        gs.fifo.setdefault(ev.note, deque()).append(note)

    def _handle_release(self, ev, key: Key) -> None:
        gs = self.groups.get(key)
        q = gs.fifo.get(ev.note) if gs else None
        target: NoteInstance | None = None
        if q:
            # FIFO：最旧的“尚按下”实例；已松键仍响者不再参与配对
            for cand in q:
                if cand.key_down:
                    target = cand
                    break
        if target is None:
            self.diagnostics.append(Diagnostic(
                code=DiagCode.UNMATCHED_RELEASE,
                tick=ev.tick,
                message=(f"松键事件找不到尚按下的匹配实例 "
                         f"(port={key[0]}, ch={key[1]}, pitch={ev.note})，"
                         f"仅诊断，不伪造音符"),
                track=ev.track, event_index=ev.event_index,
                port=key[0], channel=key[1], note=ev.note,
            ))
            return

        target.key_down = False
        target.key_release_tick = ev.tick
        target.key_release_seconds = self._seconds(ev.tick)
        target.key_release_event = ev.ref
        if target.onset_tick == ev.tick:
            self.diagnostics.append(Diagnostic(
                code=DiagCode.ZERO_LENGTH_NOTE,
                tick=ev.tick,
                message=(f"音符长度为零（按下与松键同 tick），仍保留完整事件证据: "
                         f"note_id={target.note_id}, pitch={target.pitch}"),
                track=ev.track, event_index=ev.event_index,
                port=target.port, channel=target.channel, note=target.pitch,
                detail={"note_id": target.note_id},
            ))
        self._maybe_stop_on_release(target, ev.tick, ev.ref)

    def handle_note_off(self, ev) -> None:
        self._handle_release(ev, (ev.port_at_event, ev.channel))

    def handle_sustain(self, ev) -> None:
        key = (ev.port_at_event, ev.channel)
        gs = self._group(key)
        down = ev.value >= PEDAL_THRESHOLD
        if down == gs.sustain_down:
            transition = "hold"
            stopped: list[int] = []
        elif down:
            transition = "down"
            gs.sustain_down = True
            # 踩下瞬间所有仍在响的实例（含已松键者）进入延音保持
            for note in gs.all_instances():
                if note.sounding:
                    note.sustain_held = True
                    note.sustain_held_ever = True
            stopped = []
        else:
            transition = "up"
            gs.sustain_down = False
            stopped = []
            # 抬起：停止已松键且无保持踏板捕获的实例；仍按键者只是解除保持标记
            for note in gs.all_instances():
                note.sustain_held = False
                if note.sounding and not note.key_down and not note.protected():
                    stopped.append(note.note_id)
                    self._stop(note, ev.tick, ev.ref, StopCause.SUSTAIN_UP)

        self.pedal_events.append(PedalEvent(
            ref=ev.ref, port=key[0], channel=key[1],
            pedal="sustain", control=ev.control, value=ev.value,
            transition=transition, stopped_note_ids=stopped,
            tick=ev.tick, seconds=self._seconds(ev.tick),
        ))

    def handle_sostenuto(self, ev) -> None:
        key = (ev.port_at_event, ev.channel)
        gs = self._group(key)
        down = ev.value >= PEDAL_THRESHOLD
        captured: list[int] = []
        if down == gs.sostenuto_down:
            transition = "hold"
        elif down:
            transition = "down"
            gs.sostenuto_down = True
            # 仅捕获踏下瞬间“仍按键”的实例；重复 down 不重新捕获
            for note in gs.all_instances():
                if note.sounding and note.key_down and not note.sostenuto_captured:
                    note.sostenuto_captured = True
                    note.sostenuto_captured_ever = True
                    captured.append(note.note_id)
        else:
            transition = "up"
            gs.sostenuto_down = False
            stopped: list[int] = []
            for note in gs.all_instances():
                if note.sostenuto_captured:
                    note.sostenuto_captured = False
                    # 延音可能继续保持；已松键且无其他保持才止音
                    if note.sounding and not note.key_down and not note.protected():
                        stopped.append(note.note_id)
                        self._stop(note, ev.tick, ev.ref, StopCause.SOSTENUTO_UP)
            self.pedal_events.append(PedalEvent(
                ref=ev.ref, port=key[0], channel=key[1],
                pedal="sostenuto", control=ev.control, value=ev.value,
                transition=transition, stopped_note_ids=stopped,
                tick=ev.tick, seconds=self._seconds(ev.tick),
            ))
            return

        self.pedal_events.append(PedalEvent(
            ref=ev.ref, port=key[0], channel=key[1],
            pedal="sostenuto", control=ev.control, value=ev.value,
            transition=transition, captured_note_ids=captured,
            tick=ev.tick, seconds=self._seconds(ev.tick),
        ))

    def finalize(self, eof_tick: int, eof_ref) -> None:
        """全曲末 tick 截断仍在响的实例。"""
        for note in list(self.notes):
            if note.sounding:
                self._stop(note, eof_tick, eof_ref, StopCause.EOF_TRUNCATED)
                self.diagnostics.append(Diagnostic(
                    code=DiagCode.EOF_STILL_SOUNDING,
                    tick=eof_tick,
                    message=(f"音符到全曲末 tick {eof_tick} 仍未止音，"
                             f"按计划截断（非正常释放）: note_id={note.note_id}, "
                             f"pitch={note.pitch}"),
                    port=note.port, channel=note.channel, note=note.pitch,
                    detail={"note_id": note.note_id},
                ))


def run_engine(events, tempo_map: TempoMap, eof_tick: int):
    """按全局规范顺序驱动状态机。"""
    engine = Engine(tempo_map)
    eof_ref = None

    for ev in events:
        if ev.kind is EventKind.NOTE_ON:
            engine.handle_note_on(ev)
        elif ev.kind is EventKind.NOTE_OFF:
            engine.handle_note_off(ev)
        elif ev.kind is EventKind.SUSTAIN:
            engine.handle_sustain(ev)
        elif ev.kind is EventKind.SOSTENUTO:
            engine.handle_sostenuto(ev)
        elif ev.kind is EventKind.END_OF_TRACK:
            if eof_ref is None or ev.tick == eof_tick:
                eof_ref = ev.ref
        # SET_TEMPO / MIDI_PORT / OTHER 不进入声部状态机
        # （端口已在 reader 层写入每个事件的 port_at_event）

    if eof_ref is None:
        from .model import EventRef
        eof_ref = EventRef(track=-1, event_index=-1, tick=eof_tick)

    engine.finalize(eof_tick, eof_ref)
    return engine
