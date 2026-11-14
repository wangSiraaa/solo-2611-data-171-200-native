"""测试 1：真实 fixture 端到端 + 与手工推演期望值逐项对比。

performance.mid 手工预期（tpb=480，tempo 500000@0 → 750000@960，EOF=1920）：
  id1 p60  onset 0    rel 480  end 1200 sustain_up     （CC66 捕获 + CC64 保持）
  id2 p62  onset 120  rel 650  end 1200 sustain_up     （track2 跨轨控制，CC66 捕获）
  id3 p64  onset 240  rel 720  end 1200 sustain_up
  id4 p69  onset 400  rel 440  end 440  key_release    （两踏板都未踩）
  id5 p67  onset 800  rel 900  end 1200 sustain_up     （CC66 已踩但不重新捕获）
  id6 p60  onset 1300 rel 1400 end 1400 key_release    （FIFO 重触发第一实例）
  id7 p60  onset 1340 rel 1450 end 1450 key_release    （FIFO 第二实例）
  id8 p65  onset 1340 rel 1400 end 1400 key_release ch1
  id9 p72  onset 1500 rel None end 1920 eof_truncated
诊断：unmatched_release@1550(p50)、eof_still_sounding(id9)
秒轴：960 tick = 1.0s 边界连续；1920 tick = 2.5s
"""

import unittest
from pathlib import Path

from smf_lifecycles.analyze import analyze
from smf_lifecycles.verify import verify

ROOT = Path(__file__).resolve().parents[1]
PERF = str(ROOT / "fixtures/performance.mid")
PLAIN = str(ROOT / "fixtures/plain.mid")

EXPECTED_PERF = [
    # note_id, pitch, onset, release, end, cause, ch
    (1, 60, 0, 480, 1200, "sustain_up", 0),
    (2, 62, 120, 650, 1200, "sustain_up", 0),
    (3, 64, 240, 720, 1200, "sustain_up", 0),
    (4, 69, 400, 440, 440, "key_release", 0),
    (5, 67, 800, 900, 1200, "sustain_up", 0),
    (6, 60, 1300, 1400, 1400, "key_release", 0),
    (7, 60, 1340, 1450, 1450, "key_release", 0),
    (8, 65, 1340, 1400, 1400, "key_release", 1),
    (9, 72, 1500, None, 1920, "eof_truncated", 0),
]


class FixtureTests(unittest.TestCase):
    def test_performance_lifecycles_match_hand_derived_table(self):
        r = analyze(PERF)
        self.assertEqual(len(r["notes"]), 9)
        for n, (nid, pitch, o, rel, end, cause, ch) in zip(r["notes"],
                                                            EXPECTED_PERF):
            self.assertEqual((n["note_id"], n["pitch"], n["channel"]),
                             (nid, pitch, ch))
            self.assertEqual(n["onset"]["tick"], o)
            self.assertEqual(n["key_release"]["tick"], rel)
            self.assertEqual(n["sound_end"]["tick"], end)
            self.assertEqual(n["sound_end"]["stop_cause"], cause)
            self.assertEqual(n["sound_end"]["truncated"],
                             cause == "eof_truncated")

    def test_performance_diagnostics(self):
        r = analyze(PERF)
        codes = [(d["code"], d["tick"]) for d in r["diagnostics"]]
        self.assertIn(("unmatched_release", 1550), codes)
        self.assertIn(("eof_still_sounding", 1920), codes)
        self.assertEqual(r["summary"]["truncated_notes"], 1)
        unmatched = [d for d in r["diagnostics"]
                     if d["code"] == "unmatched_release"][0]
        self.assertEqual(unmatched["note"], 50)

    def test_performance_seconds_axis(self):
        r = analyze(PERF)
        self.assertEqual(r["eof_tick"], 1920)
        self.assertEqual(r["total_seconds"], 2.5)
        self.assertTrue(r["tempo_boundary_continuity"]["continuous"])
        # tempo 边界 tick960 两侧都必须是 1.0s
        seg0, seg1 = r["tempo_map"][0], r["tempo_map"][1]
        self.assertEqual(seg0["end_seconds"], 1.0)
        self.assertEqual(seg1["start_seconds"], 1.0)
        # 抽样：tick1300 -> 1 + 340*750000/480/1e6 = 1.53125
        n6 = next(n for n in r["notes"] if n["note_id"] == 6)
        self.assertEqual(n6["onset"]["seconds"], 1.53125)

    def test_performance_pedal_evidence(self):
        r = analyze(PERF)
        pedals = {(p["pedal"], p["transition"], p["tick"]): p
                  for p in r["pedal_events"]}
        # CC66 踩下只捕获当时仍按键的 id1/2/3
        self.assertEqual(
            pedals[("sostenuto", "down", 360)]["captured_note_ids"], [1, 2, 3])
        # CC64 抬起止音 id1/2/3/5（id4 早已自然止音）
        self.assertEqual(
            pedals[("sustain", "up", 1200)]["stopped_note_ids"], [1, 2, 3, 5])
        # CC66 抬起在 CC64 踩着时不止音
        self.assertEqual(
            pedals[("sostenuto", "up", 1000)]["stopped_note_ids"], [])
        # 踏板事件来源坐标（跨轨：来自 track2）
        for p in r["pedal_events"]:
            self.assertEqual(p["event"]["track"], 2)

    def test_performance_holds_flags(self):
        r = analyze(PERF)
        by_id = {n["note_id"]: n for n in r["notes"]}
        self.assertTrue(by_id[1]["holds"]["sostenuto_captured_ever"])
        self.assertTrue(by_id[2]["holds"]["sostenuto_captured_ever"])
        # id5 在 CC66 踩下之后才按下：绝不能被保持踏板捕获
        self.assertFalse(by_id[5]["holds"]["sostenuto_captured_ever"])
        self.assertTrue(by_id[5]["holds"]["sustain_held_ever"])

    def test_plain_constant_tempo_no_pedal(self):
        r = analyze(PLAIN)
        self.assertEqual(r["total_seconds"], 1.0)  # 960 tick @ 500000
        self.assertEqual(len(r["tempo_map"]), 1)
        self.assertEqual(len(r["diagnostics"]), 0)
        n1, n2 = r["notes"]
        self.assertEqual((n1["pitch"], n1["onset"]["tick"],
                          n1["key_release"]["tick"], n1["sound_end"]["tick"]),
                         (60, 0, 480, 480))
        self.assertEqual(n1["sound_end"]["stop_cause"], "key_release")
        self.assertEqual((n2["pitch"], n2["onset"]["tick"],
                          n2["key_release"]["tick"], n2["sound_end"]["tick"]),
                         (64, 480, 960, 960))
        # 零力度 note_on 被当作松键
        self.assertEqual(n2["key_release"]["event"]["event_index"], 3)
        self.assertEqual(n2["onset"]["seconds"], 0.5)

    def test_verify_command_all_checks_pass_on_fixtures(self):
        for path in (PLAIN, PERF):
            with self.subTest(path=path):
                report = verify(path)
                self.assertTrue(report["all_passed"],
                                msg=str(report["checks"]))


if __name__ == "__main__":
    unittest.main()
