"""测试 4：CLI convert/verify 与 JSON/CSV/HTML 产物完整性。"""

import json
import tempfile
import unittest
from pathlib import Path

from midbuild import analyze_bytes, build_mid, cc, note_off, note_on, tempo
from smf_lifecycles.cli import main

ROOT = Path(__file__).resolve().parents[1]


class OutputTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)

    def _write_mid(self, name="case.mid"):
        mid = build_mid([
            [(0, tempo(500000))],
            [(0, note_on(0, 60)), (100, cc(0, 64, 127)),
             (120, note_off(0, 60)), (200, cc(0, 64, 0))],
        ], eot_tick=300)
        p = self.d / name
        mid.save(p)
        return p

    def test_convert_cli_writes_all_artifacts(self):
        mid_path = self._write_mid()
        rc = main(["convert", str(mid_path), "-o", str(self.d / "out"),
                   "--prefix", "case"])
        self.assertEqual(rc, 0)
        out = self.d / "out"
        for fname in ("case_analysis.json", "case_notes.csv",
                      "case_diagnostics.csv", "case_timeline.html"):
            self.assertTrue((out / fname).exists(), fname)

        data = json.loads((out / "case_analysis.json").read_text("utf-8"))
        # 精度说明字段存在
        self.assertIn("float64", data["seconds_precision"]["json"])
        self.assertIn("microsecond",
                      data["seconds_precision"]["csv_and_html"])
        # 整数 tick 与浮点秒同时保留
        n = data["notes"][0]
        self.assertIsInstance(n["onset"]["tick"], int)
        self.assertIsInstance(n["onset"]["seconds"], float)
        # 延音保持到 200
        self.assertEqual(n["sound_end"]["tick"], 200)
        self.assertEqual(n["sound_end"]["stop_cause"], "sustain_up")

        csv_text = (out / "case_notes.csv").read_text("utf-8")
        self.assertIn("note_id,port,channel,pitch", csv_text)
        html_text = (out / "case_timeline.html").read_text("utf-8")
        self.assertIn("<svg", html_text)
        self.assertIn("音符生命周期时间线", html_text)

    def test_verify_cli_exit_codes(self):
        mid_path = self._write_mid()
        rc_ok = main(["verify", str(mid_path)])
        self.assertEqual(rc_ok, 0)

        # 篡改期望值制造失败
        good = analyze_bytes([
            [(0, tempo(500000))],
            [(0, note_on(0, 60)), (100, cc(0, 64, 127)),
             (120, note_off(0, 60)), (200, cc(0, 64, 0))],
        ], eot_tick=300)
        good["eof_tick"] = 9999
        bad_expected = self.d / "bad.json"
        bad_expected.write_text(json.dumps(good), "utf-8")
        rc_bad = main(["verify", str(mid_path), "--expected",
                       str(bad_expected)])
        self.assertEqual(rc_bad, 1)

    def test_real_fixtures_artifacts_smoke(self):
        for stem in ("plain", "performance"):
            with self.subTest(stem=stem):
                rc = main(["convert", str(ROOT / "fixtures" / f"{stem}.mid"),
                           "-o", str(self.d / stem), "--prefix", stem])
                self.assertEqual(rc, 0)
                data = json.loads(
                    (self.d / stem / f"{stem}_analysis.json").read_text("utf-8"))
                rc_v = main(["verify", str(ROOT / "fixtures" / f"{stem}.mid")])
                self.assertEqual(rc_v, 0)
                self.assertGreater(len(data["event_stream"]), 0)


if __name__ == "__main__":
    unittest.main()
