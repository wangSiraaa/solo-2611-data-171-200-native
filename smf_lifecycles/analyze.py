"""分析编排：reader → tempo map → engine，并序列化为 JSON / CSV / HTML。

秒值精度说明：
- tick 为整数权威时间轴；秒由分段 tempo 以 Fraction 精确积分后转 float64。
- JSON 保留完整 float64 数值（约 1e-16 相对精度，受双精度表示限制）；
  CSV/HTML 按微秒（1e-6 秒）四舍五入展示，字段 seconds_precision 注明。
"""

from __future__ import annotations

import csv
import html
import json
from dataclasses import asdict
from pathlib import Path

from .engine import run_engine
from .model import DiagCode, EventKind
from .reader import parse_file
from .timebase import DEFAULT_TEMPO_US, build_tempo_map

SECONDS_PRECISION_JSON = "float64 (full double precision, ~1e-16 relative; tick is the authoritative integer axis)"
SECONDS_PRECISION_DISPLAY = "rounded to 6 decimal places (microsecond, 1e-6 s)"
DISPLAY_DECIMALS = 6


def _r(value: float | None) -> float | None:
    return None if value is None else round(float(value), DISPLAY_DECIMALS)


def _ref(ref) -> dict | None:
    if ref is None:
        return None
    return {"track": ref.track, "event_index": ref.event_index, "tick": ref.tick}


def analyze(path: str) -> dict:
    """对单个 SMF 文件执行完整分析，返回可 JSON 序列化的结果字典。"""
    parsed = parse_file(path)
    tempo_map, tempo_diags = build_tempo_map(
        parsed.events, parsed.ticks_per_beat, parsed.eof_tick)
    engine = run_engine(parsed.events, tempo_map, parsed.eof_tick)
    diagnostics = tempo_diags + engine.diagnostics

    # 诊断按规范顺序排序，保证输出确定性
    diagnostics.sort(key=lambda d: (d.tick, d.code.value,
                                    d.track if d.track is not None else -1,
                                    d.event_index if d.event_index is not None else -1))

    total_seconds = tempo_map.tick_to_seconds(parsed.eof_tick)
    continuity = tempo_map.boundary_continuity()

    result = {
        "schema_version": 1,
        "source_file": str(path),
        "smf_type": parsed.type,
        "ticks_per_beat": parsed.ticks_per_beat,
        "default_tempo_us_per_beat": DEFAULT_TEMPO_US,
        "eof_tick": parsed.eof_tick,
        "total_seconds": _r(total_seconds),
        "seconds_precision": {
            "json": SECONDS_PRECISION_JSON,
            "csv_and_html": SECONDS_PRECISION_DISPLAY,
        },
        "ordering_rule": "global sort key (absolute_tick, track_index, event_index); per-track tempo conversion is never concatenated",
        "tempo_map": [
            {
                "start_tick": s.start_tick,
                "end_tick": s.end_tick if s.end_tick is not None else parsed.eof_tick,
                "tempo_us_per_beat": s.tempo_us,
                "bpm": round(60_000_000 / s.tempo_us, 6),
                "start_seconds": _r(s.start_seconds),
                "end_seconds": _r(s.end_seconds),
                "source_event": _ref(s.source),
            }
            for s in tempo_map.segments
        ],
        "tempo_boundary_continuity": {
            "continuous": len(continuity) == 0,
            "violations": continuity,
        },
        "notes": [_note_dict(n) for n in engine.notes],
        "pedal_events": [_pedal_dict(p) for p in engine.pedal_events],
        "diagnostics": [_diag_dict(d) for d in diagnostics],
        "event_stream": [
            _event_dict(e, tempo_map.tick_to_seconds(e.tick))
            for e in parsed.events
            if e.kind in (EventKind.NOTE_ON, EventKind.NOTE_OFF,
                          EventKind.SUSTAIN, EventKind.SOSTENUTO,
                          EventKind.SET_TEMPO, EventKind.MIDI_PORT)
        ],
        "summary": {
            "track_count": parsed.track_count,
            "note_count": len(engine.notes),
            "truncated_notes": sum(1 for n in engine.notes if n.truncated),
            "pedal_event_count": len(engine.pedal_events),
            "diagnostic_counts": {
                code.value: sum(1 for d in diagnostics if d.code is code)
                for code in DiagCode
            },
        },
    }
    return result


def _note_dict(n) -> dict:
    return {
        "note_id": n.note_id,
        "port": n.port,
        "channel": n.channel,
        "pitch": n.pitch,
        "velocity": n.velocity,
        "onset": {
            "tick": n.onset_tick,
            "seconds": n.onset_seconds,
            "event": _ref(n.onset_event),
        },
        "key_release": {
            "tick": n.key_release_tick,
            "seconds": n.key_release_seconds,
            "event": _ref(n.key_release_event),
        },
        "sound_end": {
            "tick": n.sound_end_tick,
            "seconds": n.sound_end_seconds,
            "event": _ref(n.sound_end_event),
            "stop_cause": n.stop_cause.value if n.stop_cause else None,
            "truncated": n.truncated,
        },
        "holds": {
            "sustain_held_ever": n.sustain_held_ever,
            "sostenuto_captured_ever": n.sostenuto_captured_ever,
        },
    }


def _pedal_dict(p) -> dict:
    out = asdict(p)
    out["event"] = _ref(p.ref)
    out["seconds_display"] = _r(p.seconds)
    return out


def _diag_dict(d) -> dict:
    out = asdict(d)
    out["code"] = d.code.value
    return out


def _event_dict(e, seconds: float) -> dict:
    return {
        "tick": e.tick,
        "seconds": seconds,
        "track": e.track,
        "event_index": e.event_index,
        "kind": e.kind.value,
        "port": e.port_at_event,
        "channel": e.channel,
        "note": e.note,
        "velocity": e.velocity,
        "control": e.control,
        "value": e.value,
        "tempo_us": e.tempo_us,
        "raw_type": e.raw_type,
    }


# ---- 输出落盘 ----

def write_json(result: dict, path: str | Path) -> None:
    Path(path).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(result: dict, notes_path: str | Path,
              diagnostics_path: str | Path) -> None:
    note_fields = [
        "note_id", "port", "channel", "pitch", "velocity",
        "onset_tick", "onset_seconds",
        "key_release_tick", "key_release_seconds",
        "sound_end_tick", "sound_end_seconds",
        "stop_cause", "truncated",
        "onset_event", "key_release_event", "sound_end_event",
    ]
    with open(notes_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(note_fields)
        for n in result["notes"]:
            w.writerow([
                n["note_id"], n["port"], n["channel"], n["pitch"], n["velocity"],
                n["onset"]["tick"], _r(n["onset"]["seconds"]),
                n["key_release"]["tick"], _r(n["key_release"]["seconds"]),
                n["sound_end"]["tick"], _r(n["sound_end"]["seconds"]),
                n["sound_end"]["stop_cause"], n["sound_end"]["truncated"],
                _loc(n["onset"]["event"]), _loc(n["key_release"]["event"]),
                _loc(n["sound_end"]["event"]),
            ])

    diag_fields = ["code", "tick", "track", "event_index", "port",
                   "channel", "note", "message"]
    with open(diagnostics_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(diag_fields)
        for d in result["diagnostics"]:
            w.writerow([d[k] for k in diag_fields])


def _loc(ref) -> str:
    if not ref:
        return ""
    return f"t{ref['track']}e{ref['event_index']}@{ref['tick']}"


def write_html(result: dict, path: str | Path) -> None:
    Path(path).write_text(_render_html(result), encoding="utf-8")


def _render_html(result: dict) -> str:
    width, pad_l, pad_r, pad_t = 980, 150, 30, 70
    plot_w = width - pad_l - pad_r
    total_s = max(result["total_seconds"], 1e-9)

    def x(seconds: float | None) -> float:
        if seconds is None:
            return pad_l
        return pad_l + max(0.0, min(1.0, seconds / total_s)) * plot_w

    groups = sorted({(n["port"], n["channel"]) for n in result["notes"]})
    height = pad_t + len(groups) * 64 + 150
    parts: list[str] = []
    parts.append(
        f"<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<title>{html.escape(result['source_file'])} 时间线</title>"
        f"<style>{_CSS}</style></head><body>")
    parts.append("<h1>音符生命周期时间线</h1>")
    parts.append(
        f"<p class='meta'>{html.escape(result['source_file'])} · "
        f"SMF type {result['smf_type']} · tpb={result['ticks_per_beat']} · "
        f"EOF tick {result['eof_tick']} · 总时长 {result['total_seconds']}s "
        f"（秒轴 {html.escape(result['seconds_precision']['csv_and_html'])}）</p>")

    parts.append(f"<svg width='{width}' height='{height}' "
                 f"viewBox='0 0 {width} {height}' font-family='monospace'>")

    # 秒轴刻度（每 0.5s）
    axis_y = pad_t - 18
    parts.append(f"<line x1='{pad_l}' y1='{axis_y}' x2='{width-pad_r}' "
                 f"y2='{axis_y}' stroke='#888'/>")
    step = 0.5 if total_s <= 6 else 1.0
    t = 0.0
    while t <= total_s + 1e-9:
        xx = x(t)
        parts.append(f"<line x1='{xx:.2f}' y1='{axis_y-4}' x2='{xx:.2f}' "
                     f"y2='{height-90}' stroke='#eee'/>")
        parts.append(f"<text x='{xx:.2f}' y='{axis_y-8}' font-size='10' "
                     f"text-anchor='middle' fill='#666'>{t:.1f}s</text>")
        t += step

    # tempo 边界竖线
    for seg in result["tempo_map"][1:]:
        xx = x(seg["start_seconds"])
        parts.append(f"<line x1='{xx:.2f}' y1='{pad_t-26}' x2='{xx:.2f}' "
                     f"y2='{height-90}' stroke='#e0a030' stroke-dasharray='4 3'/>")
        parts.append(f"<text x='{xx+3:.2f}' y='{pad_t-28}' font-size='9' "
                     f"fill='#b07020'>tempo {seg['tempo_us_per_beat']}µs "
                     f"({seg['bpm']} BPM)</text>")

    lane_h = 56
    for li, (port, ch) in enumerate(groups):
        top = pad_t + li * (lane_h + 8)
        parts.append(f"<rect x='10' y='{top}' width='{width-20}' "
                     f"height='{lane_h}' fill='#fafafa' stroke='#ddd'/>")
        parts.append(f"<text x='18' y='{top+20}' font-size='11' fill='#333'>"
                     f"port {port} / ch {ch}</text>")
        key_y, snd_y = top + 30, top + 46
        parts.append(f"<text x='90' y='{key_y+4}' font-size='10' fill='#888'>"
                     f"按键期</text>")
        parts.append(f"<text x='90' y='{snd_y+4}' font-size='10' fill='#888'>"
                     f"发声期</text>")

        lane_notes = [n for n in result["notes"]
                      if (n["port"], n["channel"]) == (port, ch)]
        for n in lane_notes:
            x0 = x(n["onset"]["seconds"])
            xk = x(n["key_release"]["seconds"])
            xe = x(n["sound_end"]["seconds"])
            color = "#c0392b" if n["sound_end"]["truncated"] else "#2c6fbb"
            parts.append(
                f"<line x1='{x0:.2f}' y1='{key_y}' x2='{xk:.2f}' y2='{key_y}' "
                f"stroke='#444' stroke-width='3'/>")
            parts.append(
                f"<line x1='{x0:.2f}' y1='{snd_y}' x2='{xe:.2f}' y2='{snd_y}' "
                f"stroke='{color}' stroke-width='4'/>")
            parts.append(
                f"<text x='{x0:.2f}' y='{top+14}' font-size='9' fill='#333'>"
                f"#{n['note_id']} P{n['pitch']}</text>")

        # 踏板事件标记
        for p in result["pedal_events"]:
            if (p["port"], p["channel"]) != (port, ch):
                continue
            xx = x(p["seconds"])
            if p["pedal"] == "sustain":
                col, mark = "#27784a", "S64"
            else:
                col, mark = "#7d3c98", "H66"
            if p["transition"] == "down":
                sym = "▼"
            elif p["transition"] == "up":
                sym = "▲"
            else:
                sym = "·"
            parts.append(
                f"<text x='{xx:.2f}' y='{top+lane_h-4}' font-size='10' "
                f"fill='{col}' text-anchor='middle'>{mark}{sym}</text>")

    # 图例与诊断
    legend_y = height - 70
    parts.append(
        f"<text x='18' y='{legend_y}' font-size='11' fill='#333'>"
        f"图例：<tspan fill='#2c6fbb'>蓝色=正常止音</tspan>　"
        f"<tspan fill='#c0392b'>红色=EOF 截断</tspan>　"
        f"S64▼/▲=延音踏板 踩/抬　H66▼/▲=保持踏板 踩/抬　"
        f"橙色虚线=tempo 边界</text>")
    if result["diagnostics"]:
        parts.append(f"<text x='18' y='{legend_y+22}' font-size='11' "
                     f"fill='#b03030'>诊断（{len(result['diagnostics'])}）：</text>")
        for i, d in enumerate(result["diagnostics"][:12]):
            parts.append(
                f"<text x='30' y='{legend_y+38+i*14}' font-size='10' "
                f"fill='#994444'>[{d['code']}] tick {d['tick']}: "
                f"{html.escape(d['message'])}</text>")
    parts.append("</svg></body></html>")
    return "".join(parts)


_CSS = """
body{font-family:sans-serif;margin:24px;background:#fff;color:#222}
h1{font-size:18px} .meta{font-size:12px;color:#555}
svg{border:1px solid #ddd;border-radius:6px;background:#fff}
"""
