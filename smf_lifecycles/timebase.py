"""tick → 秒 的分段 tempo 映射。

口径（rules.json: seconds / same_tick_tempo）：
- 全局 tempo 事件统一建立一张映射表，绝不按轨分别换秒后拼接。
- 同 tick 出现不同 tempo 值属于冲突：必须诊断，并按规范同 tick 顺序
  （track index, event index）的首个事件生效，不依赖文件读取偶然顺序。
  同值重复事件折叠为同一段。
- 首个 tempo 事件之前使用 default_tempo_us_per_beat=500000。
- 秒值通过整数 tick 跨度积分得到：span_seconds = span_ticks * tempo_us
  / ticks_per_beat / 1_000_000，分段起点时间用 Fraction 精确累加，
  查询时再转 float，边界因此严格连续。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .model import DiagCode, Diagnostic, EventRef, TempoSegment

DEFAULT_TEMPO_US = 500_000


@dataclass
class TempoPoint:
    tick: int
    tempo_us: int
    source: EventRef


class TempoMap:
    def __init__(self, ticks_per_beat: int, segments: list[TempoSegment],
                 points: list[TempoPoint], eof_tick: int):
        self.ticks_per_beat = ticks_per_beat
        self.segments = segments
        self.points = points
        self.eof_tick = eof_tick

    def tick_to_seconds(self, tick: int) -> float:
        """整数 tick → 秒。分段线性，边界处与相邻段给出同一数值。"""
        tick = int(tick)
        for seg in self.segments:
            end = seg.end_tick if seg.end_tick is not None else self.eof_tick
            if seg.start_tick <= tick <= end:
                span = tick - seg.start_tick
                frac = (Fraction(seg.start_seconds)
                        + Fraction(span * seg.tempo_us,
                                   self.ticks_per_beat * 1_000_000))
                return float(frac)
        # 兜底（理论上不可达）：按最后一段外推
        seg = self.segments[-1]
        span = tick - seg.start_tick
        return float(Fraction(seg.start_seconds)
                     + Fraction(span * seg.tempo_us,
                                self.ticks_per_beat * 1_000_000))

    def boundary_continuity(self) -> list[dict]:
        """检查每段边界两侧秒值是否严格相等。"""
        out: list[dict] = []
        for left, right in zip(self.segments, self.segments[1:]):
            t = right.start_tick
            a = self._segment_seconds(left, t)
            b = float(Fraction(right.start_seconds))
            if abs(a - b) > 0.0:
                out.append({"tick": t, "left_seconds": a,
                            "right_seconds": b, "gap": b - a})
        return out

    def _segment_seconds(self, seg: TempoSegment, tick: int) -> float:
        span = tick - seg.start_tick
        return float(Fraction(seg.start_seconds)
                     + Fraction(span * seg.tempo_us,
                                self.ticks_per_beat * 1_000_000))


def build_tempo_map(events, ticks_per_beat: int, eof_tick: int):
    """从全局事件流构建 tempo 映射，同时产出冲突诊断。

    返回 (TempoMap, list[Diagnostic])。
    """
    diagnostics: list[Diagnostic] = []

    # 事件流已按 (tick, track, event_index) 排序，同 tick 规范顺序天然成立
    raw_points: list[TempoPoint] = []
    for ev in events:
        if ev.kind.name == "SET_TEMPO":
            raw_points.append(TempoPoint(
                tick=ev.tick, tempo_us=ev.tempo_us, source=ev.ref))

    # 冲突诊断 + 同值折叠
    points: list[TempoPoint] = []
    by_tick: dict[int, list[TempoPoint]] = {}
    for p in raw_points:
        by_tick.setdefault(p.tick, []).append(p)

    for tick in sorted(by_tick):
        group = by_tick[tick]
        distinct = sorted({p.tempo_us for p in group})
        if len(distinct) > 1:
            winner = group[0]  # 规范顺序的首个事件生效
            diagnostics.append(Diagnostic(
                code=DiagCode.TEMPO_CONFLICT,
                tick=tick,
                message=(f"tick {tick} 存在 {len(group)} 个不同 tempo 值 "
                         f"{distinct} µs/beat；按同 tick 顺序采用 "
                         f"{winner.tempo_us}（track {winner.source.track}, "
                         f"event {winner.source.event_index}）"),
                detail={
                    "candidate_values": distinct,
                    "candidates": [
                        {"track": p.source.track,
                         "event_index": p.source.event_index,
                         "tempo_us": p.tempo_us} for p in group
                    ],
                    "winner": {"track": winner.source.track,
                               "event_index": winner.source.event_index,
                               "tempo_us": winner.tempo_us},
                },
            ))
            points.append(winner)
        else:
            # 同值重复：折叠，保留规范顺序首个事件作为来源
            points.append(group[0])

    # 分段：每段 [start_tick, next_start_tick)，最后一段到 eof_tick。
    # 折叠规则（rules.json: duplicates equal values may collapse）：
    # 1) 边界与当前段起点重合（典型：tick 0 的首个 tempo）且同值 → 跳过；
    # 2) 相邻段 tempo 相同 → 合并为一段，不产生无意义的边界。
    # 不同值即使同 tick 也保留，并已在上面产出冲突诊断。
    boundaries: list[int] = [0]
    tempos: list[int] = [DEFAULT_TEMPO_US]
    sources: list[EventRef | None] = [None]
    for p in points:
        if p.tick == boundaries[-1] and p.tempo_us == tempos[-1]:
            continue
        if p.tempo_us == tempos[-1]:
            continue  # 与前一段同值：折叠，保留更早的来源事件
        boundaries.append(p.tick)
        tempos.append(p.tempo_us)
        sources.append(p.source)

    segments: list[TempoSegment] = []
    clock = Fraction(0)
    for i, (start, tempo) in enumerate(zip(boundaries, tempos)):
        is_last = i + 1 == len(boundaries)
        end = eof_tick if is_last else boundaries[i + 1]
        end_seconds = float(
            clock + Fraction((end - start) * tempo,
                             ticks_per_beat * 1_000_000))
        segments.append(TempoSegment(
            start_tick=start,
            end_tick=None if is_last else end,
            tempo_us=tempo,
            start_seconds=float(clock),
            end_seconds=end_seconds,
            source=sources[i],
        ))
        clock += Fraction((end - start) * tempo,
                          ticks_per_beat * 1_000_000)

    return TempoMap(ticks_per_beat, segments, points, eof_tick), diagnostics
