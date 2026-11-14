"""核心数据结构。

设计约定：
- tick 一律保留为整数，是权威时间轴；秒为派生量（浮点）。
- 每个事件保留来源轨索引 track 与轨内原始事件索引 event_index。
- 同 tick 事件的全局顺序由 (tick, track, event_index) 唯一确定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class EventKind(str, Enum):
    """本工具关心的事件类别。"""

    NOTE_ON = "note_on"
    NOTE_OFF = "note_off"
    SUSTAIN = "cc64_sustain"
    SOSTENUTO = "cc66_sostenuto"
    SET_TEMPO = "set_tempo"
    MIDI_PORT = "midi_port"
    END_OF_TRACK = "end_of_track"
    OTHER = "other"


@dataclass(frozen=True)
class EventRef:
    """事件在原文件中的定位坐标。"""

    track: int
    event_index: int
    tick: int


@dataclass
class MidiEvent:
    """统一排序后的单个事件。"""

    ref: EventRef
    kind: EventKind
    channel: int | None = None
    port_at_event: int | None = None
    note: int | None = None
    velocity: int | None = None
    control: int | None = None
    value: int | None = None
    tempo_us: int | None = None
    raw_type: str = ""

    @property
    def tick(self) -> int:
        return self.ref.tick

    @property
    def track(self) -> int:
        return self.ref.track

    @property
    def event_index(self) -> int:
        return self.ref.event_index


class StopCause(str, Enum):
    """音符实际结束发声的原因。"""

    KEY_RELEASE = "key_release"          # 松键且无任何踏板保持
    SUSTAIN_UP = "sustain_up"            # 延音踏板抬起导致止音
    SOSTENUTO_UP = "sostenuto_up"        # 保持踏板抬起导致止音（且延音未保持）
    EOF_TRUNCATED = "eof_truncated"      # 全曲结束仍在响，按计划截断


class DiagCode(str, Enum):
    TEMPO_CONFLICT = "tempo_conflict"
    UNMATCHED_RELEASE = "unmatched_release"
    EOF_STILL_SOUNDING = "eof_still_sounding"
    ZERO_LENGTH_NOTE = "zero_length_note"


@dataclass
class Diagnostic:
    """诊断记录：不静默丢弃异常输入。"""

    code: DiagCode
    tick: int
    message: str
    track: int | None = None
    event_index: int | None = None
    port: int | None = None
    channel: int | None = None
    note: int | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class NoteInstance:
    """一个同音高按键实例（重触发即产生新实例，不覆盖旧音）。"""

    note_id: int
    port: int
    channel: int
    pitch: int

    # 权威整数 tick 时间轴
    onset_tick: int
    key_release_tick: int | None = None
    sound_end_tick: int | None = None

    # 派生秒轴（微秒精度输出，见 seconds_precision 说明）
    onset_seconds: float | None = None
    key_release_seconds: float | None = None
    sound_end_seconds: float | None = None

    velocity: int | None = None
    key_down: bool = True
    sounding: bool = True

    # 踏板保持标记（当前状态 + 生命周期内是否曾被保持）
    sustain_held: bool = False
    sostenuto_captured: bool = False
    sustain_held_ever: bool = False
    sostenuto_captured_ever: bool = False

    # 事件证据（定位坐标）
    onset_event: EventRef | None = None
    key_release_event: EventRef | None = None
    sound_end_event: EventRef | None = None
    stop_cause: StopCause | None = None
    truncated: bool = False

    def protected(self) -> bool:
        """当前是否被任一踏板保持。"""
        return self.sustain_held or self.sostenuto_captured


@dataclass
class PedalEvent:
    """CC64/CC66 控制事件证据。"""

    ref: EventRef
    port: int
    channel: int
    pedal: str          # "sustain" | "sostenuto"
    control: int
    value: int
    transition: str     # "down" | "up" | "hold"
    stopped_note_ids: list[int] = field(default_factory=list)
    captured_note_ids: list[int] = field(default_factory=list)
    tick: int = 0
    seconds: float = 0.0


@dataclass
class TempoSegment:
    """分段 tempo 映射中的一段 [start_tick, end_tick)。"""

    start_tick: int
    end_tick: int | None      # None 表示延伸到曲末
    tempo_us: int
    start_seconds: float
    end_seconds: float | None
    source: EventRef | None   # 生效 tempo 的来源事件
