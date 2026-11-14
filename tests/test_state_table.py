"""测试 2：手工微型序列状态表。

不依赖大文件，直接对构造事件断言每个时刻的完整状态表，
重点覆盖：
A) 同 tick 事件按 (track, event_index) 顺序处理；
B) CC66 保持踏板只在踏下瞬间捕获仍按键实例；
C) 零长度音符保留证据；
D) 松键 FIFO 与重触发不覆盖旧音。
"""

import unittest

import mido

from midbuild import analyze_bytes, cc, note_off, note_on, port, tempo


def by_id(result):
    return {n["note_id"]: n for n in result["notes"]}


class HandStateTableTests(unittest.TestCase):

    def test_sostenuto_capture_moment_state_table(self):
        """逐 tick 核对 CC66 捕获时刻的 key_down/sounding/捕获集合。"""
        t0 = [  # 轨0：tempo
            (0, tempo(500000)),
        ]
        t1 = [  # 轨1
            (0, note_on(0, 60)),       # A 按下
            (100, note_on(0, 60)),     # B 重触发（A 仍按键）
            (200, cc(0, 66, 127)),     # CC66 踩下：捕获 A、B
            (240, note_on(0, 72)),     # C 在踏板踩着时按下：不得被捕获
            (300, note_off(0, 60)),    # 松 A（FIFO 最旧仍按键者）
            (400, cc(0, 66, 0)),       # CC66 抬：A 止音；B 仍按键继续响
            (500, note_off(0, 60)),    # 松 B
            (600, note_off(0, 72)),    # 松 C
        ]
        r = analyze_bytes([t0, t1])
        notes = by_id(r)
        a, b, c = notes[1], notes[2], notes[3]

        # CC66 down 证据：只捕获 A、B（当时仍按键），C 尚不存在
        ped = next(p for p in r["pedal_events"]
                   if p["transition"] == "down" and p["pedal"] == "sostenuto")
        self.assertEqual(ped["captured_note_ids"], [1, 2])
        self.assertEqual(ped["tick"], 200)
        self.assertFalse(c["holds"]["sostenuto_captured_ever"])
        self.assertTrue(a["holds"]["sostenuto_captured_ever"])
        self.assertTrue(b["holds"]["sostenuto_captured_ever"])

        # A：300 松键但被 CC66 保持，400 踏板抬起才止音
        self.assertEqual((a["key_release"]["tick"], a["sound_end"]["tick"],
                          a["sound_end"]["stop_cause"]),
                         (300, 400, "sostenuto_up"))
        # B：400 时仍按键，踏板抬起不影响；500 松键即止音
        self.assertEqual((b["key_release"]["tick"], b["sound_end"]["tick"],
                          b["sound_end"]["stop_cause"]),
                         (500, 500, "key_release"))
        # C：从未被捕获，600 松键即止音
        self.assertEqual((c["key_release"]["tick"], c["sound_end"]["tick"]),
                         (600, 600))

    def test_same_tick_track_then_event_order(self):
        """同 tick 两轨事件：track0 先于 track1；同轨按 event_index。"""
        t0 = [
            (0, tempo(500000)),
            (100, cc(0, 64, 127)),                 # 轨0 先踩延音
        ]
        t1 = [
            (100, note_on(0, 60)),                 # 轨1 同 tick 后按下
            (200, note_off(0, 60)),                # 松键：延音踩着 -> 继续响
            (300, cc(0, 64, 0)),                   # 轨0 抬延音 -> 止音
        ]
        r = analyze_bytes([t0, t1])
        n = r["notes"][0]
        self.assertEqual(n["sound_end"]["tick"], 300)
        self.assertEqual(n["sound_end"]["stop_cause"], "sustain_up")
        # 事件流同 tick 顺序断言
        ev = [(e["tick"], e["track"], e["event_index"])
              for e in r["event_stream"]]
        self.assertEqual(ev, sorted(ev))
        at100 = [e for e in r["event_stream"] if e["tick"] == 100]
        self.assertEqual([(e["track"], e["kind"]) for e in at100],
                         [(0, "cc64_sustain"), (1, "note_on")])

    def test_zero_length_note_kept_with_evidence(self):
        t0 = [(0, tempo(500000))]
        t1 = [
            (0, note_on(0, 60)),
            (0, note_off(0, 60)),       # 同 tick 松键：长度 0
        ]
        r = analyze_bytes([t0, t1], eot_tick=100)
        self.assertEqual(len(r["notes"]), 1)
        n = r["notes"][0]
        self.assertEqual(n["onset"]["tick"], 0)
        self.assertEqual(n["key_release"]["tick"], 0)
        self.assertEqual(n["sound_end"]["tick"], 0)
        self.assertIsNotNone(n["onset"]["event"])
        self.assertIsNotNone(n["key_release"]["event"])
        codes = [d["code"] for d in r["diagnostics"]]
        self.assertIn("zero_length_note", codes)

    def test_fifo_retrigger_does_not_overwrite(self):
        t0 = [(0, tempo(500000))]
        t1 = [
            (0, note_on(0, 60, 90)),     # A
            (50, note_on(0, 60, 70)),    # B 重触发，A 仍按键
            (100, note_off(0, 60)),      # 配给 A
            (150, note_off(0, 60)),      # 配给 B
        ]
        r = analyze_bytes([t0, t1], eot_tick=200)
        a, b = r["notes"]
        self.assertEqual(a["velocity"], 90)
        self.assertEqual(b["velocity"], 70)
        self.assertEqual((a["key_release"]["tick"], b["key_release"]["tick"]),
                         (100, 150))
        # 各自独立止音事件，不覆盖
        self.assertNotEqual(a["sound_end"]["event"]["event_index"],
                            b["sound_end"]["event"]["event_index"])

    def test_unmatched_release_diagnostic_only(self):
        t0 = [(0, tempo(500000))]
        t1 = [(0, note_off(0, 60)),     # 无对应按下
              (100, note_on(0, 60)),
              (200, note_off(0, 60))]
        r = analyze_bytes([t0, t1], eot_tick=300)
        self.assertEqual(len(r["notes"]), 1)       # 不伪造音符
        self.assertIn("unmatched_release",
                      [d["code"] for d in r["diagnostics"]])

    def test_port_meta_scopes_channel_state(self):
        """midi_port 是轨本地状态；同 channel 不同 port 的状态互不影响。"""
        t0 = [(0, tempo(500000))]
        t1 = [
            (0, port(0)),
            (0, cc(0, 64, 127)),                  # port0 ch0 延音
            (10, note_on(0, 60)),
            (20, note_off(0, 60)),                # port0：被延音保持
        ]
        t2 = [
            (0, port(1)),
            (0, note_on(0, 60)),                  # port1 ch0 同音高，独立实例
            (30, note_off(0, 60)),                # port1：无踏板，立即止音
        ]
        r = analyze_bytes([t0, t1, t2], eot_tick=400)
        groups = {(n["port"], n["pitch"]): n for n in r["notes"]}
        self.assertEqual(groups[(0, 60)]["sound_end"]["tick"], 400)  # EOF 截断
        self.assertTrue(groups[(0, 60)]["sound_end"]["truncated"])
        self.assertEqual(groups[(1, 60)]["sound_end"]["tick"], 30)
        self.assertEqual(groups[(1, 60)]["sound_end"]["stop_cause"],
                         "key_release")

    def test_early_track_end_does_not_silence_shared_channel(self):
        """track0 在 200 结束；track1 共享 (port0,ch0) 的音必须响到全局 EOT。"""
        import tempfile
        from pathlib import Path

        tpb = 480
        mid = mido.MidiFile(type=1, ticks_per_beat=tpb)

        t0 = mido.MidiTrack()
        t0.append(mido.MetaMessage("set_tempo", tempo=500000, time=0))
        t0.append(mido.MetaMessage("end_of_track", time=200))   # 提前结束

        t1 = mido.MidiTrack()
        t1.append(mido.Message("note_on", channel=0, note=60,
                               velocity=80, time=0))
        t1.append(mido.MetaMessage("end_of_track", time=400))   # 全局 EOT=400

        mid.tracks = [t0, t1]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "early_eot.mid"
            mid.save(p)
            from smf_lifecycles.analyze import analyze
            r = analyze(str(p))

        self.assertEqual(r["eof_tick"], 400)
        n = r["notes"][0]
        self.assertIsNone(n["key_release"]["tick"])
        self.assertEqual(n["sound_end"]["tick"], 400)
        self.assertTrue(n["sound_end"]["truncated"])
        # 不能在 track0 的 200 tick 处被静音
        self.assertNotEqual(n["sound_end"]["tick"], 200)


if __name__ == "__main__":
    unittest.main()
