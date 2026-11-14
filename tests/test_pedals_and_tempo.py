"""测试 3：两踏板叠加规则与 tempo 秒轴连续性。"""

import unittest
from fractions import Fraction

from midbuild import TPB, analyze_bytes, cc, note_off, note_on, tempo
from smf_lifecycles.reader import parse_file
from smf_lifecycles.timebase import build_tempo_map


class PedalCombinationTests(unittest.TestCase):

    def test_sustain_up_while_sostenuto_holds_keeps_sounding(self):
        """延音先抬、保持踏板仍捕获：不止音；保持踏板再抬才止音。"""
        t0 = [(0, tempo(500000))]
        t1 = [
            (0, note_on(0, 60)),
            (100, cc(0, 66, 127)),    # 保持踏板踩下，捕获 id1
            (150, cc(0, 64, 127)),    # 延音也踩下
            (200, note_off(0, 60)),   # 松键，双踏板保持
            (300, cc(0, 64, 0)),      # 延音抬：保持踏板仍在 -> 继续响
            (400, cc(0, 66, 0)),      # 保持踏板抬：止音
        ]
        r = analyze_bytes([t0, t1], eot_tick=500)
        n = r["notes"][0]
        self.assertEqual(n["key_release"]["tick"], 200)
        self.assertEqual(n["sound_end"]["tick"], 400)
        self.assertEqual(n["sound_end"]["stop_cause"], "sostenuto_up")
        pedals = {(p["pedal"], p["transition"], p["tick"]):
                  p["stopped_note_ids"] for p in r["pedal_events"]}
        self.assertEqual(pedals[("sustain", "up", 300)], [])
        self.assertEqual(pedals[("sostenuto", "up", 400)], [1])

    def test_sostenuto_up_while_sustain_holds_keeps_sounding(self):
        """保持踏板先抬、延音仍踩着：不止音；延音再抬才止音（fixture 同构）。"""
        t0 = [(0, tempo(500000))]
        t1 = [
            (0, note_on(0, 60)),
            (50, cc(0, 66, 127)),
            (100, cc(0, 64, 127)),
            (150, note_off(0, 60)),
            (200, cc(0, 66, 0)),      # 保持踏板抬，延音保持中
            (300, cc(0, 64, 0)),      # 延音抬，止音
        ]
        r = analyze_bytes([t0, t1], eot_tick=400)
        n = r["notes"][0]
        self.assertEqual(n["sound_end"]["tick"], 300)
        self.assertEqual(n["sound_end"]["stop_cause"], "sustain_up")

    def test_retrigger_while_sustain_down_both_queued(self):
        """延音踩着时同音高重触发：旧实例松键后仍在 FIFO 中且仍响。"""
        t0 = [(0, tempo(500000))]
        t1 = [
            (0, cc(0, 64, 127)),
            (0, note_on(0, 60, 80)),     # A
            (100, note_off(0, 60)),      # A 松键，延音保持（仍占 FIFO）
            (120, note_on(0, 60, 90)),   # B 重触发
            (200, note_off(0, 60)),      # 配给 B（A 已松键不再参与配对）
            (300, cc(0, 64, 0)),         # 同时止音 A、B
        ]
        r = analyze_bytes([t0, t1], eot_tick=400)
        a, b = r["notes"]
        self.assertEqual((a["key_release"]["tick"], b["key_release"]["tick"]),
                         (100, 200))
        self.assertEqual(a["sound_end"]["tick"], b["sound_end"]["tick"])
        up = next(p for p in r["pedal_events"] if p["transition"] == "up")
        self.assertEqual(sorted(up["stopped_note_ids"]), [1, 2])

    def test_hold_pedal_events_are_evidence_only(self):
        """同方向重复 CC 记为 hold，不重新捕获。"""
        t0 = [(0, tempo(500000))]
        t1 = [
            (0, note_on(0, 60)),
            (50, cc(0, 66, 127)),
            (80, cc(0, 66, 100)),       # hold，不重新捕获
            (100, note_on(0, 64)),      # 新音，hold 期间不被捕获
            (150, cc(0, 66, 0)),
            (200, note_off(0, 60)),
            (220, note_off(0, 64)),
        ]
        r = analyze_bytes([t0, t1], eot_tick=300)
        trans = [(p["transition"], p["captured_note_ids"])
                 for p in r["pedal_events"]]
        self.assertEqual(trans, [("down", [1]), ("hold", []), ("up", [])])
        p64 = next(n for n in r["notes"] if n["pitch"] == 64)
        self.assertFalse(p64["holds"]["sostenuto_captured_ever"])


class TempoContinuityTests(unittest.TestCase):

    def _map(self, track_specs, eot=1920):
        import tempfile
        from pathlib import Path
        from midbuild import build_mid
        mid = build_mid(track_specs, TPB, eot)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.mid"
            mid.save(p)
            parsed = parse_file(str(p))
        tmap, diags = build_tempo_map(parsed.events, TPB, parsed.eof_tick)
        return tmap, diags

    def test_seconds_continuous_across_tempo_boundary(self):
        tmap, _ = self._map([
            [(0, tempo(500000)), (960, tempo(750000))],
        ])
        self.assertEqual(tmap.boundary_continuity(), [])
        # 边界两侧严格同一秒值
        self.assertEqual(tmap.tick_to_seconds(959), 959 / TPB * 0.5)
        self.assertEqual(tmap.tick_to_seconds(960), 1.0)
        self.assertEqual(tmap.tick_to_seconds(961),
                         1.0 + 1 / TPB * 0.75)
        # 与手工 Fraction 积分一致
        expected = Fraction(1) + Fraction(960 * 750000, TPB * 1_000_000)
        self.assertAlmostEqual(tmap.tick_to_seconds(1920), float(expected))

    def test_tempo_conflict_diagnosed_and_canonical_winner(self):
        """同 tick 不同 tempo：诊断且按 (track, event) 顺序的首个生效。"""
        tmap, diags = self._map([
            [(480, tempo(400000))],                    # track0 先
            [(480, tempo(600000))],                    # track1 后（冲突）
        ])
        self.assertEqual(len(diags), 1)
        d = diags[0]
        self.assertEqual(d.code.value, "tempo_conflict")
        self.assertEqual(d.detail["winner"]["tempo_us"], 400000)
        self.assertEqual(d.detail["winner"]["track"], 0)
        # 生效段确认为 400000
        seg = next(s for s in tmap.segments if s.start_tick == 480)
        self.assertEqual(seg.tempo_us, 400000)
        # 秒轴仍然连续
        self.assertEqual(tmap.boundary_continuity(), [])

    def test_equal_tempo_duplicates_collapse(self):
        tmap, diags = self._map([
            [(480, tempo(500000))],
            [(480, tempo(500000))],
        ])
        self.assertEqual(diags, [])
        self.assertEqual([s.tempo_us for s in tmap.segments], [500000])

    def test_default_tempo_before_first_event(self):
        tmap, _ = self._map([
            [(480, tempo(250000))],
        ])
        self.assertEqual(tmap.segments[0].tempo_us, 500000)
        self.assertEqual(tmap.tick_to_seconds(480), 0.5)


if __name__ == "__main__":
    unittest.main()
