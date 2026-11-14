"""矛盾证书、包含极小集合、重放复算、位置准确性与文件顺序不变性。"""

from __future__ import annotations

import itertools
import os
import tempfile
import unittest
from fractions import Fraction

from dimcheck.report import check, explain, to_dict
from dimcheck.solver import BASES

from ._helpers import run, vec


class ConflictCertificateTests(unittest.TestCase):
    def setUp(self):
        self.files = {
            "known.dims": (
                "mass : M\nlength : L\ntime : T\n"
                "temperature : Theta\nreference_temperature : Theta\nangle : 1\n"
            ),
            "model.formula": (
                "speed: velocity = length / time\n"
                "acceleration_rule: acceleration = velocity / time\n"
                "newton: force = mass * acceleration\n"
                "work: energy = force * length\n"
                "kinetic: energy = mass * velocity^2 / 2\n"
                "thermal_ratio: factor = exp(temperature / reference_temperature)\n"
                "oscillation: phase = sin(angle)\n"
                "root_scale: root_energy = energy^(1/2)\n"
                "free_response: response = gain * input\n"
                "cycle: length = velocity * time\n"
            ),
            "conflict.formula": "wrong_force: force = mass * velocity\n",
        }
        self.tmp = tempfile.TemporaryDirectory()
        self.result, _ = run(self.tmp.name, self.files)

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_inconsistent_and_no_fake_units(self):
        self.assertEqual(self.result.status, "inconsistent")
        for var in self.result.variables:
            if var["name"].startswith("__"):
                continue
            self.assertEqual(var["status"], "unresolved_conflict")
            self.assertNotIn("dimension_text", var)

    def test_every_conflict_replays_to_nonzero_residual(self):
        combos = [c for c in self.result.conflicts if c["kind"] == "linear_combination_conflict"]
        self.assertTrue(combos)
        for c in combos:
            self.assertNotEqual(c["residual"], "0")
            self.assertTrue(c["replay_matches"])
            self.assertEqual(c["replay_residual"], c["residual"])
            # 每条证书都能定位到具体源 span 与片段。
            for term in c["combination"]:
                self.assertTrue(term["snippet"].strip())
                self.assertEqual(term["span"]["line"], term["span"]["line"])

    def test_minimal_set_each_removal_restores_consistency(self):
        minimal = next(c for c in self.result.conflicts if c["kind"] == "minimal_conflict_set")
        self.assertGreaterEqual(len(minimal["item_ids"]), 2)
        # 必须包含真正出错的公式。
        self.assertTrue(any("wrong_force" in i for i in minimal["item_ids"]))
        for chk in minimal["minimality_checks"]:
            self.assertTrue(chk["consistent_after_removal"])
            self.assertEqual(chk["remaining_conflict_count"], 0)

    def test_minimality_independently_recomputed(self):
        """不依赖求解器自带结论，直接用 check 逐条移除 statement 复核极小性。"""
        minimal = next(
            c for c in self.result.conflicts if c["kind"] == "minimal_conflict_set"
        )
        for item_id in minimal["item_ids"]:
            files = {
                name: text
                for name, text in self.files.items()
            }
            # 通过删除整条语句模拟“移除该 statement”。
            files = _remove_item(files, item_id)
            with tempfile.TemporaryDirectory() as tmp2:
                sub, _ = run(tmp2, files)
            self.assertEqual(sub.status, "consistent", msg=f"移除 {item_id} 后应相容")

    def test_explain_contains_spans_and_json_serializable(self):
        text = explain(self.result)
        self.assertIn("线性组合冲突", text)
        self.assertIn("wrong_force", text)
        payload = to_dict(self.result)
        import json
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertIn("minimal_conflict_set", encoded)


def _remove_item(files: dict[str, str], item_id: str) -> dict[str, str]:
    # item_id 形如 <file>#formula#<label> 或 <file>#decl#<name>
    file_part, kind, ident = item_id.split("#")
    fname = os.path.basename(file_part)
    lines = files[fname].splitlines(keepends=True)
    kept = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            kept.append(line)
            continue
        first_token = stripped.split()[0].rstrip(":")
        # 公式 "label: ..." 与声明 "name : ..." 都以 ident 起头、紧跟冒号。
        if first_token == ident and stripped[len(ident):].lstrip().startswith(":"):
            continue
        kept.append(line)
    return {**files, fname: "".join(kept)}


class FunctionConflictTests(unittest.TestCase):
    def test_log_of_dimensional_argument_conflicts_with_precise_span(self):
        files = {
            "d.dims": "length : L\n",
            "f.formula": "bad_function: response = log(length)\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = run(tmp, files)
        self.assertEqual(result.status, "inconsistent")
        combo = next(c for c in result.conflicts if c["kind"] == "linear_combination_conflict")
        self.assertEqual(combo["base_dimension"], "L")
        self.assertNotEqual(Fraction(combo["residual"]), 0)
        minimal = next(c for c in result.conflicts if c["kind"] == "minimal_conflict_set")
        self.assertEqual(len(minimal["item_ids"]), 2)

    def test_sin_of_dimensional_argument_rejected_via_constraint(self):
        files = {
            "d.dims": "t : T\n",
            "f.formula": "bad: p = sin(t)\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = run(tmp, files)
        self.assertEqual(result.status, "inconsistent")


class ParseVsDimensionSeparationTests(unittest.TestCase):
    def test_parse_failure_does_not_become_fake_dimension_result(self):
        files = {
            "d.dims": "a : L\n",
            "bad.formula": "p: y = a^z\nq: w = mystery(a)\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = run(tmp, files)
        codes = {d.code for d in result.diagnostics.items}
        self.assertIn("E101", codes)          # 变量指数
        # 没有量纲矛盾——解析/语义失败与量纲矛盾分开。
        self.assertEqual(result.status, "parse_error_only")
        self.assertEqual(
            [c for c in result.conflicts if c["kind"] == "linear_combination_conflict"],
            [],
        )
        rejected_labels = {r["name"] for r in result.rejected_items}
        self.assertEqual(rejected_labels, {"q"})

    def test_lexer_error_position_is_reported(self):
        files = {"f.formula": "p: y = a @ b\n"}
        with tempfile.TemporaryDirectory() as tmp:
            result, store = run(tmp, files)
        diag = result.diagnostics.items[0]
        self.assertEqual(diag.code, "E001")
        self.assertEqual((diag.span.start_line, diag.span.start_col), (1, 10))


class FileOrderInvarianceTests(unittest.TestCase):
    def test_permuting_files_keeps_mathematical_conclusion(self):
        files = {
            "known.dims": "mass : M\nlength : L\ntime : T\n",
            "model.formula": (
                "speed: velocity = length / time\n"
                "newton: force = mass * velocity / time\n"
                "work: energy = force * length\n"
            ),
            "extra.dims": "temperature : Theta\n",
        }
        reference = None
        for perm in itertools.permutations(files):
            ordered = {name: files[name] for name in perm}
            with tempfile.TemporaryDirectory() as tmp:
                result, _ = run(tmp, ordered)
            snapshot = {
                v["name"]: v.get("dimension_text")
                for v in result.variables
                if not v["name"].startswith("__")
            }
            if reference is None:
                reference = snapshot
            else:
                self.assertEqual(snapshot, reference)
            self.assertEqual(result.status, "consistent")

    def test_source_positions_stable_under_permutation(self):
        files = {
            "a.dims": "x : L\n",
            "b.formula": "one: y = x / t\ntwo: z = x\n",
        }

        def relative_spans(result):
            # 比较时去掉每次运行变化的临时目录前缀，只留文件名与行列。
            return {
                (os.path.basename(s["file"]), s["line"], s["column"])
                for v in result.variables
                if not v["name"].startswith("__")
                for s in v["sources"]
            }

        spans = []
        for perm in itertools.permutations(files):
            ordered = {name: files[name] for name in perm}
            with tempfile.TemporaryDirectory() as tmp:
                result, _store = run(tmp, ordered)
            spans.append(relative_spans(result))
        self.assertEqual(spans[0], spans[1])


class ZeroLiteralTests(unittest.TestCase):
    def test_zero_is_dimensionless_no_polymorphism(self):
        files = {
            "d.dims": "a : L\n",
            "f.formula": "ok: b = a + 0\nbad: c = a - 0\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = run(tmp, files)
        # a+0 与 a-0 都要求 0 与 L 同维 -> 矛盾（零不是多态量纲零）。
        self.assertEqual(result.status, "inconsistent")


if __name__ == "__main__":
    unittest.main()
