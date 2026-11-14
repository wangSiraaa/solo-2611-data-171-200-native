"""边界场景：空/纯注释输入、sqrt 与幂等价、括号化指数、独立零空间核验。"""

from __future__ import annotations

import tempfile
import unittest
from fractions import Fraction

from dimcheck.report import check
from dimcheck.solver import _scalar_rows

from ._helpers import meta, run, var_entry, vec


class EmptyInputTests(unittest.TestCase):
    def test_empty_and_comment_only_files(self):
        files = {"empty.dims": "", "notes.formula": "# 只有注释\n\n   # 另一行\n"}
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = run(tmp, files)
        self.assertEqual(result.status, "consistent")
        self.assertEqual(result.diagnostics.items, [])
        self.assertEqual(meta(result)["scalar_degrees_of_freedom"], 0)

    def test_completely_unknown_variable_is_free_not_dimensionless(self):
        files = {"f.formula": "only: z = q\n"}
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = run(tmp, files)
        # z 与 q 共享一个自由向量，绝不能被当成无量纲。
        self.assertEqual(var_entry(result, "q")["status"], "free")
        self.assertEqual(meta(result)["scalar_degrees_of_freedom"], 4)


class SqrtPowerEquivalenceTests(unittest.TestCase):
    def test_sqrt_matches_half_power(self):
        files = {
            "d.dims": "a : M L^2\n",
            "f.formula": "p: x = sqrt(a)\nq: y = a^(1/2)\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = run(tmp, files)
        self.assertEqual(result.status, "consistent")
        self.assertEqual(vec(result, "x"), vec(result, "y"))
        self.assertEqual(vec(result, "x"), (Fraction(1, 2), 1, 0, 0))

    def test_integer_power_two(self):
        files = {"d.dims": "v : L T^-1\n", "f.formula": "k: e = v^2\n"}
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = run(tmp, files)
        self.assertEqual(vec(result, "e"), (0, 2, -2, 0))

    def test_negative_power_is_inversion(self):
        files = {"d.dims": "v : L T^-1\n", "f.formula": "k: e = v^-2\n"}
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = run(tmp, files)
        self.assertEqual(vec(result, "e"), (0, -2, 2, 0))


class IndependentNullspaceVerificationTests(unittest.TestCase):
    def test_nullspace_basis_independently_satisfies_homogeneous_system(self):
        # response = gain * input：两个独立自由维，零空间基必须逐行满足齐次方程。
        files = {"f.formula": "r: response = gain * input\n"}
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = run(tmp, files)
        sy, sol = result.system, result.solve
        self.assertEqual(len(sol.nullspace), 2)
        index = {name: j for j, name in enumerate(sy.variables)}
        for basis in sol.nullspace:
            for component in range(4):
                for row in _scalar_rows(sy)[component]:
                    total = sum(
                        row.coeff[j] * basis[sy.variables[j]][component]
                        for j in range(len(sy.variables))
                    )
                    self.assertEqual(total, 0)
        # 零空间基线性无关：在 input 与 response 上的系数矩阵满秩。
        cols = [
            (b["input"][0], b["response"][0]) for b in sol.nullspace
        ]
        determinant = cols[0][0] * cols[1][1] - cols[0][1] * cols[1][0]
        self.assertNotEqual(determinant, 0)


class ParametricTextTests(unittest.TestCase):
    def test_parametric_representation_lists_all_bases(self):
        files = {"f.formula": "r: response = gain * input\n"}
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = run(tmp, files)
        gain = var_entry(result, "gain")
        params = set(gain["parametric"]["parameters"])
        self.assertEqual(params, {"k0", "k1"})
        for expr in gain["parametric"]["per_base_component"].values():
            self.assertTrue(expr.strip())


if __name__ == "__main__":
    unittest.main()
