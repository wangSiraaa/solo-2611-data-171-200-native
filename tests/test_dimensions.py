"""物理关系、自由增益、循环、有理幂、函数限制与温标的量纲测试。

每个相容用例都独立把推断向量代回全部原始标量约束核验；不直接信任求解器。
"""

from __future__ import annotations

import unittest
from fractions import Fraction

from dimcheck.report import check
from dimcheck.solver import _scalar_rows

from ._helpers import meta, run, var_entry, vec


def substitute_all_constraints(result) -> None:
    """把特解 + 零空间的任意参数代回每条标量行，要求残差恒为 0。"""
    sy = result.system
    sol = result.solve
    self_index = {name: j for j, name in enumerate(sy.variables)}
    blocks = _scalar_rows(sy)

    def eval_row(row, assignment):
        lhs = sum(row.coeff[j] * assignment[sy.variables[j]] for j in range(len(sy.variables)))
        return lhs - row.rhs

    # 1) 特解必须满足全部行。
    for component, block in enumerate(blocks):
        for row in block:
            assignment = {name: sol.particular[name][component] for name in sy.variables}
            assert eval_row(row, assignment) == 0

    # 2) 每个零空间基向量（齐次，RHS 视为 0）也必须满足全部行。
    for basis in sol.nullspace:
        for component, block in enumerate(blocks):
            for row in block:
                total = sum(
                    row.coeff[j] * basis[sy.variables[j]][component]
                    for j in range(len(sy.variables))
                )
                assert total == 0

    # 3) 特解 + 任意有理线性组合（取参数 3/7、-2/5 等）仍满足。
    params = [Fraction(3, 7), Fraction(-2, 5), Fraction(11)]
    for component, block in enumerate(blocks):
        for row in block:
            assignment = {}
            for name in sy.variables:
                value = sol.particular[name][component]
                for p, basis in zip(params, sol.nullspace):
                    value += p * basis[name][component]
                assignment[name] = value
            assert eval_row(row, assignment) == 0


class TinyHandCheckTests(unittest.TestCase):
    def test_tiny_speed_is_length_over_time_cycle_consistent(self):
        files = {
            "tiny.dims": "distance : L\nduration : T\n",
            "tiny.formula": (
                "rate: speed = distance/duration\n"
                "back: distance = speed*duration\n"
            ),
        }
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result, _store = run(tmp, files)
        self.assertEqual(result.status, "consistent")
        # 手工核对：speed = L T^-1。
        self.assertEqual(vec(result, "speed"), (0, 1, -1, 0))
        self.assertEqual(vec(result, "distance"), (0, 1, 0, 0))
        self.assertEqual(vec(result, "duration"), (0, 0, 1, 0))
        self.assertEqual(meta(result)["scalar_degrees_of_freedom"], 0)
        substitute_all_constraints(result)


class ModelPhysicsTests(unittest.TestCase):
    FILES = {
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
    }

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.result, self.store = run(self._tmp.name, self.FILES)

    def tearDown(self):
        self._tmp.cleanup()

    def test_known_declared_dimensions(self):
        self.assertEqual(vec(self.result, "mass"), (1, 0, 0, 0))
        self.assertEqual(vec(self.result, "length"), (0, 1, 0, 0))
        self.assertEqual(vec(self.result, "time"), (0, 0, 1, 0))
        self.assertEqual(vec(self.result, "temperature"), (0, 0, 0, 1))
        self.assertEqual(vec(self.result, "angle"), (0, 0, 0, 0))

    def test_inferred_kinematic_and_dynamic_chain(self):
        self.assertEqual(vec(self.result, "velocity"), (0, 1, -1, 0))
        self.assertEqual(vec(self.result, "acceleration"), (0, 1, -2, 0))
        self.assertEqual(vec(self.result, "force"), (1, 1, -2, 0))
        self.assertEqual(vec(self.result, "energy"), (1, 2, -2, 0))

    def test_rational_power_sqrt_energy(self):
        self.assertEqual(
            vec(self.result, "root_energy"),
            (Fraction(1, 2), 1, -1, 0),
        )

    def test_dimensionless_functions_output_one(self):
        self.assertEqual(vec(self.result, "factor"), (0, 0, 0, 0))
        self.assertEqual(vec(self.result, "phase"), (0, 0, 0, 0))

    def test_temperature_only_compared_with_temperature(self):
        # temperature / reference_temperature 必须无量纲才能进 exp。
        self.assertEqual(vec(self.result, "temperature"), vec(self.result, "reference_temperature"))

    def test_free_gain_keeps_free_dimensions_not_defaulted_to_one(self):
        entry = var_entry(self.result, "input")
        self.assertEqual(entry["status"], "free")
        self.assertIsNotNone(entry["parametric"])
        # 欠定变量绝不能被静默当作无量纲：参数化表达必须含自由参数 k0。
        self.assertIn("k0", entry["parametric"]["parameters"])
        self.assertTrue(
            any(expr.strip() != "0" for expr in entry["parametric"]["per_base_component"].values())
        )
        # response = gain * input 含两个独立自由维：input 与 response 本身。
        self.assertEqual(meta(self.result)["free_dimension_variables"], ["input"])
        self.assertEqual(meta(self.result)["nullspace_rank"], 2)
        self.assertEqual(meta(self.result)["scalar_degrees_of_freedom"], 8)
        # gain 与 response 通过 response = gain*input 共享同一自由结构。
        self.assertEqual(var_entry(self.result, "gain")["status"], "free")
        self.assertEqual(var_entry(self.result, "response")["status"], "free")

    def test_cycle_is_simultaneous_constraint_not_rejected(self):
        labels = {item.name for item in self.result.system.items if item.kind == "formula"}
        self.assertIn("cycle", labels)
        self.assertTrue(all(item.accepted for item in self.result.system.items))

    def test_full_substitution_verifies_every_constraint(self):
        substitute_all_constraints(self.result)

    def test_dependencies_point_to_real_sources(self):
        entry = var_entry(self.result, "force")
        deps = entry["depends_on_items"]
        self.assertTrue(any("newton" in d for d in deps))
        self.assertTrue(any(d.endswith("#decl#time") for d in deps))


class AdditiveCompatibilityTests(unittest.TestCase):
    def test_addition_of_different_dimensions_conflicts(self):
        import tempfile
        files = {
            "d.dims": "a : L\nb : T\n",
            "f.formula": "bad: c = a + b\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = run(tmp, files)
        self.assertEqual(result.status, "inconsistent")
        self.assertTrue(result.conflicts)


class CrossFileSameNameTests(unittest.TestCase):
    def test_same_name_across_files_shares_one_vector_and_conflicts(self):
        import tempfile
        files = {
            "a.dims": "x : L\n",
            "b.dims": "x : T\n",  # 同名变量必须共用同一维数 -> 矛盾
        }
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = run(tmp, files)
        self.assertEqual(result.status, "inconsistent")
        minimal = next(c for c in result.conflicts if c["kind"] == "minimal_conflict_set")
        ids = set(minimal["item_ids"])
        self.assertEqual(len(ids), 2)
        # 两个声明各自都是极小集合的一员，删任一即相容。
        self.assertTrue(all(chk["consistent_after_removal"] for chk in minimal["minimality_checks"]))

    def test_same_name_consistent_across_files(self):
        import tempfile
        # f2 是普通公式 z = x，两文件共享同一个 x 向量。
        files = {
            "a.dims": "x : L\n",
            "f1.formula": "one: y = x / z\n",
            "f2.formula": "two: z = x\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = run(tmp, files)
        self.assertEqual(result.status, "consistent")
        self.assertEqual(vec(result, "z"), (0, 1, 0, 0))


if __name__ == "__main__":
    unittest.main()
