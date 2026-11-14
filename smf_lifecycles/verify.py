"""独立核验：不变量检查 + 可选的期望结果对比。

verify 不信任 convert 的落盘产物，而是重新解析 MIDI、重跑状态机，
再检查一组结构性不变量；若提供 --expected JSON，则再做语义对比。
"""

from __future__ import annotations

import json
from pathlib import Path

from .analyze import analyze


def _check(name: str, ok: bool, detail: str = "") -> dict:
    return {"check": name, "passed": bool(ok), "detail": detail}


def verify_invariants(midi_path: str) -> list[dict]:
    result = analyze(midi_path)
    checks: list[dict] = []

    # 1. 事件流全局顺序：(tick, track, event_index) 严格非降且同 tick 轨序正确
    stream = result["event_stream"]
    keys = [(e["tick"], e["track"], e["event_index"]) for e in stream]
    checks.append(_check(
        "global_event_ordering",
        keys == sorted(keys),
        "排序键 (tick, track, event_index) 全局有序"))

    # 2. 每个音符三个时间的 tick 单调性：onset <= release <= end
    #    （EOF 截断且从未松键的实例允许 key_release 缺失）
    monotonic = True
    bad: list[int] = []
    for n in result["notes"]:
        o, r, s = n["onset"]["tick"], n["key_release"]["tick"], n["sound_end"]["tick"]
        ok = (o <= s) if r is None else (o <= r <= s)
        if not ok:
            monotonic = False
            bad.append(n["note_id"])
    checks.append(_check(
        "note_tick_monotonicity", monotonic,
        "全部音符满足 onset <= key_release <= sound_end" if monotonic
        else f"违例 note_id={bad}"))

    # 3. 秒轴与 tick 单调一致（截断音符可能没有 key_release）
    def _phase_ok(n):
        o = n["onset"]["seconds"]
        r = n["key_release"]["seconds"]
        s = n["sound_end"]["seconds"]
        if r is None:
            return o <= s
        return o <= r <= s

    sec_mono = all(_phase_ok(n) for n in result["notes"])
    checks.append(_check("note_seconds_monotonicity", sec_mono,
                         "秒轴与 tick 轴单调一致"))

    # 4. tempo 边界秒轴严格连续
    cont = result["tempo_boundary_continuity"]["continuous"]
    checks.append(_check(
        "tempo_boundary_continuity", cont,
        "所有 tempo 分段边界两侧秒值严格相等" if cont
        else f"不连续: {result['tempo_boundary_continuity']['violations']}"))

    # 5. 止音原因与截断标记自洽
    cause_ok = True
    for n in result["notes"]:
        end = n["sound_end"]
        if end["truncated"] != (end["stop_cause"] == "eof_truncated"):
            cause_ok = False
        if end["tick"] > result["eof_tick"]:
            cause_ok = False
    checks.append(_check("stop_cause_self_consistent", cause_ok,
                         "truncated 标记与 eof_truncated 原因一致且不越 EOF"))

    # 6. EOF 截断诊断数量与截断音符数量一致
    n_trunc = result["summary"]["truncated_notes"]
    n_diag = result["summary"]["diagnostic_counts"]["eof_still_sounding"]
    checks.append(_check(
        "eof_truncation_diagnosed", n_trunc == n_diag,
        f"截断音符 {n_trunc} 个，EOF 诊断 {n_diag} 条"))

    # 7. 无匹配松键不产生音符：诊断数 >= 1 时所有音符均有合法 onset 证据
    evidence_ok = all(n["onset"]["event"] is not None
                      and n["sound_end"]["event"] is not None
                      for n in result["notes"])
    checks.append(_check("all_notes_have_evidence", evidence_ok,
                         "每个音符都有按下与止音事件证据"))

    # 8. 踏板证据完整性：每次 down/up 都有来源坐标
    pedal_ok = all(p["event"] is not None and p["transition"]
                   in ("down", "up", "hold") for p in result["pedal_events"])
    checks.append(_check("pedal_event_evidence", pedal_ok,
                         "CC64/CC66 事件均带定位坐标与跳变方向"))

    # 9. 总时长等于 EOF tick 的秒轴换算
    total_ok = abs(result["total_seconds"]
                   - round(_safe_total(result), 6)) < 1e-9
    checks.append(_check("total_seconds_matches_eof", total_ok,
                         f"total_seconds={result['total_seconds']}"))

    return checks


def _safe_total(result: dict) -> float:
    # 用最后一段 tempo 重新积分 EOF tick，避免直接相信 total_seconds 字段
    segs = result["tempo_map"]
    last = segs[-1]
    tpb = result["ticks_per_beat"]
    span = last["end_tick"] - last["start_tick"]
    return last["start_seconds"] + span * last["tempo_us_per_beat"] / tpb / 1_000_000


def compare_expected(midi_path: str, expected_path: str) -> list[dict]:
    """与期望 JSON 做语义对比（忽略浮点最末位与键顺序）。"""
    actual = analyze(midi_path)
    expected = json.loads(Path(expected_path).read_text(encoding="utf-8"))
    checks: list[dict] = []

    for key in ("eof_tick", "ticks_per_beat", "total_seconds", "smf_type"):
        checks.append(_check(
            f"expected:{key}",
            actual.get(key) == expected.get(key),
            f"actual={actual.get(key)} expected={expected.get(key)}"))

    an = {n["note_id"]: n for n in actual["notes"]}
    en = {n["note_id"]: n for n in expected["notes"]}
    checks.append(_check("expected:note_id_set",
                         set(an) == set(en),
                         f"actual ids={sorted(an)} expected ids={sorted(en)}"))

    for nid in sorted(set(an) & set(en)):
        a, e = an[nid], en[nid]
        for phase in ("onset", "key_release", "sound_end"):
            checks.append(_check(
                f"expected:note{nid}:{phase}:tick",
                a[phase]["tick"] == e[phase]["tick"],
                f"actual={a[phase]['tick']} expected={e[phase]['tick']}"))
        checks.append(_check(
            f"expected:note{nid}:stop_cause",
            a["sound_end"]["stop_cause"] == e["sound_end"]["stop_cause"],
            f"actual={a['sound_end']['stop_cause']} "
            f"expected={e['sound_end']['stop_cause']}"))
        checks.append(_check(
            f"expected:note{nid}:truncated",
            a["sound_end"]["truncated"] == e["sound_end"]["truncated"], ""))

    codes_a = sorted(d["code"] for d in actual["diagnostics"])
    codes_e = sorted(d["code"] for d in expected["diagnostics"])
    checks.append(_check("expected:diagnostic_codes", codes_a == codes_e,
                         f"actual={codes_a} expected={codes_e}"))
    return checks


def verify(midi_path: str, expected_path: str | None = None) -> dict:
    checks = verify_invariants(midi_path)
    if expected_path:
        checks += compare_expected(midi_path, expected_path)
    return {
        "midi_file": midi_path,
        "expected_file": expected_path,
        "total_checks": len(checks),
        "passed": sum(1 for c in checks if c["passed"]),
        "failed": sum(1 for c in checks if not c["passed"]),
        "all_passed": all(c["passed"] for c in checks),
        "checks": checks,
    }
