"""Validation for the NMO pipeline.

Runs against the real fixture material (non-zero delay, +/- offsets,
one dead trace) plus synthetic traces with hand-computed expectations.
No plots: fold/stack are checked by independent per-sample loops.

Run from the repository root:  python -m unittest discover tests -v
"""

from __future__ import annotations

import csv
import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

import nmo
from nmo import (
    Plan,
    Reason,
    TraceRecord,
    VelocityFunction,
    correct,
    correct_cdp,
    explain_sample,
    is_stretch_muted,
    load_plan,
    load_velocity,
    sample_at,
    stretch_factor,
)
from nmo.errors import (
    HeaderContradictionError,
    NonMonotonicMappingError,
    PlanError,
    UnsupportedFormatError,
    VelocityTableError,
)
from nmo.export import export_all

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures"
SEGY = str(FIX / "gathers.sgy")
VELCSV = str(FIX / "velocity.csv")
PLANJSON = str(FIX / "plan.json")

# byte offsets in the fixture file (SEG-Y rev1, big endian)
BIN_FMT = 3224          # binary header: sample format code (int16)
TRACE0_HDR = 3600       # first trace header
TRACE_NS = 114          # 0-based offset of samples-per-trace in trace header
TRACE_DT = 116          # 0-based offset of sample interval in trace header


def corrupted_copy(offset: int, value: int, dst_dir: str) -> str:
    """Copy gathers.sgy with one big-endian int16 header field changed."""
    data = bytearray(Path(SEGY).read_bytes())
    data[offset:offset + 2] = int(value).to_bytes(2, "big", signed=True)
    dst = str(Path(dst_dir) / "corrupted.sgy")
    Path(dst).write_bytes(bytes(data))
    return dst


def make_plan(**kw) -> Plan:
    args = dict(t0_start_s=0.0, t0_step_s=0.002, output_count=21,
                stretch_limit=0.35)
    args.update(kw)
    return Plan(**args)


def make_trace(file_index=0, cdp=1, offset=0.0, delay_s=0.0, dt_s=0.002,
               samples=None, ns=21, live=True, tracl=None) -> TraceRecord:
    if samples is None:
        samples = np.arange(ns, dtype=np.float64)
    return TraceRecord(
        file_index=file_index, tracl=tracl or file_index + 1, cdp=cdp,
        offset_m=offset, live=live, delay_s=delay_s, dt_s=dt_s,
        samples=np.asarray(samples, dtype=np.float64),
    )


def const_vel(cdp=1, v=2000.0, t0_max=1.0) -> VelocityFunction:
    return VelocityFunction(cdp=cdp,
                            t0_s=np.array([0.0, t0_max]),
                            vrms_mps=np.array([v, v]))


class TestRealMaterial(unittest.TestCase):
    """Validation on fixtures/gathers.sgy + velocity.csv + plan.json."""

    @classmethod
    def setUpClass(cls):
        cls.plan = load_plan(PLANJSON)
        cls.result = correct(SEGY, VELCSV, cls.plan)

    def test_grouping_by_true_cdp_not_file_order(self):
        res = self.result.cdps[101]
        # file order is scrambled; rows sorted by (|offset|, offset)
        self.assertEqual(res.trace_indices.tolist(), [9, 3, 7, 15, 10, 14])
        self.assertEqual(res.offsets_m.tolist(),
                         [0.0, 150.0, -300.0, 300.0, 600.0, 900.0])
        self.assertEqual(self.result.cdp_ids(), [101, 102, 103])
        for cdp, res in self.result.cdps.items():
            self.assertEqual(res.corrected.shape, (6, 360))
            self.assertTrue(np.allclose(res.t0_s, self.plan.t0_grid))

    def test_dead_trace_excluded_and_reported(self):
        res = self.result.cdps[102]
        j = int(np.flatnonzero(res.trace_indices == 5)[0])
        self.assertFalse(bool(res.live[j]))
        self.assertTrue(np.all(res.reason[j] == Reason.DEAD_TRACE))
        self.assertTrue(np.all(np.isnan(res.corrected[j])))
        self.assertFalse(np.any(res.valid[j]))
        self.assertLessEqual(int(res.fold.max()), 5)  # 6 traces, 1 dead
        self.assertEqual(self.result.excluded_traces,
                         [{"file_index": 5, "tracl": 6, "cdp": 102,
                           "offset_m": 300.0, "reason": "DEAD_TRACE"}])

    def test_negative_offset_same_mapping_identity_kept(self):
        res = self.result.cdps[101]
        jn = int(np.flatnonzero(res.trace_indices == 7)[0])   # -300 m
        jp = int(np.flatnonzero(res.trace_indices == 15)[0])  # +300 m
        np.testing.assert_array_equal(res.tx_s[jn], res.tx_s[jp])
        np.testing.assert_array_equal(res.valid[jn], res.valid[jp])
        np.testing.assert_array_equal(res.reason[jn], res.reason[jp])
        np.testing.assert_array_equal(res.stretch[jn], res.stretch[jp])
        # sign and identity preserved
        self.assertEqual(res.offsets_m[jn], -300.0)
        self.assertEqual(res.offsets_m[jp], +300.0)
        self.assertNotEqual(res.tracl[jn], res.tracl[jp])

    def test_nonzero_delay_shifts_source_sampling(self):
        # trace 10: cdp 101, offset 600 m, delay 20 ms, dt 2000 us
        res = self.result.cdps[101]
        j = int(np.flatnonzero(res.trace_indices == 10)[0])
        tr = self.result.traces_by_cdp[101][j]
        self.assertAlmostEqual(tr.delay_s, 0.020)
        self.assertAlmostEqual(tr.dt_s, 0.002)
        self.assertAlmostEqual(tr.time_first_s, 0.020)
        self.assertAlmostEqual(tr.time_last_s, 0.020 + 419 * 0.002)
        # every OK sample must equal scalar sampling with the shifted axis
        for i in range(res.t0_s.size):
            if not res.valid[j, i]:
                continue
            hit = sample_at(tr.samples, tr.dt_s, tr.delay_s, res.tx_s[j, i])
            self.assertIsNotNone(hit)
            self.assertAlmostEqual(res.corrected[j, i], hit[0], places=15)
        # and the delay really moves the fractional index (10 samples)
        i = 300
        r_delayed = (res.tx_s[j, i] - 0.020) / 0.002
        r_nodelay = (res.tx_s[j, i] - 0.0) / 0.002
        self.assertAlmostEqual(r_nodelay - r_delayed, 10.0)

    def test_out_of_support_suffix_for_far_offset(self):
        # cdp 101, offset 900 m: tx exceeds the 0.838 s record at large t0
        res = self.result.cdps[101]
        j = int(np.flatnonzero(res.trace_indices == 14)[0])
        oos = np.flatnonzero(res.reason[j] == Reason.OUT_OF_SUPPORT)
        self.assertEqual(oos.size, 23)  # matches traces.csv accounting
        self.assertTrue(np.all(oos == np.arange(337, 360)))  # a true suffix
        self.assertTrue(np.all(np.isnan(res.corrected[j, oos])))
        self.assertFalse(np.any(res.valid[j, oos]))
        # the last in-support sample really is inside
        self.assertEqual(res.reason[j, 336], Reason.OK)
        self.assertLessEqual(res.tx_s[j, 336], 0.838)
        self.assertGreater(res.tx_s[j, 337], 0.838)

    def test_stretch_mute_prefix_and_t0_zero_rule(self):
        res = self.result.cdps[101]
        j = int(np.flatnonzero(res.trace_indices == 14)[0])  # 900 m
        # stretch decreases with t0 (constant v): mute zone is a prefix
        muted = np.flatnonzero(res.reason[j] == Reason.STRETCH_MUTE)
        self.assertTrue(np.all(muted == np.arange(0, 276)))
        self.assertEqual(res.reason[j, 276], Reason.OK)
        # t0 == 0 with offset != 0 -> infinite stretch -> muted
        self.assertTrue(np.isinf(res.stretch[j, 0]))
        # t0 == 0 with offset == 0 -> stretch 0, kept, exact first sample
        j0 = int(np.flatnonzero(res.trace_indices == 9)[0])
        self.assertEqual(res.stretch[j0, 0], 0.0)
        self.assertEqual(res.reason[j0, 0], Reason.OK)
        tr = self.result.traces_by_cdp[101][j0]
        self.assertAlmostEqual(res.corrected[j0, 0], tr.samples[0])

    def test_fold_and_stack_by_independent_accumulation(self):
        for cdp, res in self.result.cdps.items():
            for i in range(res.t0_s.size):
                n = 0
                acc = 0.0
                for jj in range(res.trace_indices.size):
                    if res.valid[jj, i]:
                        acc += float(res.corrected[jj, i])
                        n += 1
                self.assertEqual(n, int(res.fold[i]), f"cdp {cdp} t0[{i}]")
                self.assertTrue(
                    math.isclose(acc, float(res.stack_sum[i]),
                                 rel_tol=1e-12, abs_tol=1e-18),
                    f"cdp {cdp} t0[{i}] sum")
                if n == 0:
                    self.assertTrue(np.isnan(res.stack[i]))
                else:
                    self.assertTrue(
                        math.isclose(acc / n, float(res.stack[i]),
                                     rel_tol=1e-12, abs_tol=1e-18))

    def test_no_zero_fill_of_missing_samples(self):
        for res in self.result.cdps.values():
            self.assertTrue(np.all(np.isnan(res.corrected[~res.valid])))
            # stack_sum must be the sum over valid samples only
            np.testing.assert_allclose(
                res.stack_sum,
                np.where(res.valid, res.corrected, 0.0).sum(axis=0),
                rtol=1e-12, atol=1e-18)

    def test_explain_sample_across_reason_types(self):
        cases = [
            (101, 9, 0, "OK"),            # zero offset, t0 = 0
            (101, 14, 0, "STRETCH_MUTE"),  # t0=0, offset 900
            (101, 14, 200, "STRETCH_MUTE"),
            (101, 14, 300, "OK"),
            (101, 14, 359, "OUT_OF_SUPPORT"),
            (102, 5, 100, "DEAD_TRACE"),
            (103, 16, 250, "OK"),          # negative offset
            (101, 10, 300, "OK"),          # 20 ms delay trace
        ]
        for cdp, fi, ti, want in cases:
            info = explain_sample(self.result, cdp, fi, ti)
            self.assertEqual(info["reason"], want, (cdp, fi, ti))
            res = self.result.cdps[cdp]
            j = int(np.flatnonzero(res.trace_indices == fi)[0])
            self.assertEqual(info["valid"], bool(res.valid[j, ti]))
            if want == "OK":
                src = info["source"]
                tr = self.result.traces_by_cdp[cdp][j]
                r = (info["tx_s"] - tr.delay_s) / tr.dt_s
                self.assertAlmostEqual(src["fractional_index"], r)
                self.assertEqual(src["k"], int(math.floor(r)))
                self.assertAlmostEqual(
                    src["weight_k"] + src["weight_k1"], 1.0)
                manual = (src["weight_k"] * tr.samples[src["k"]]
                          + src["weight_k1"] * tr.samples[src["k1"]])
                self.assertAlmostEqual(manual, info["amplitude"], places=15)
                self.assertAlmostEqual(info["amplitude"],
                                       res.corrected[j, ti], places=15)

    def test_velocity_used_matches_table(self):
        res = self.result.cdps[103]
        # nodes 2200, 2200, 2300, 2350 at 0, 0.25, 0.65, 0.8
        self.assertAlmostEqual(res.velocity_mps[0], 2200.0)
        self.assertAlmostEqual(res.velocity_mps[125], 2200.0)   # t0=0.25
        self.assertAlmostEqual(res.velocity_mps[325], 2300.0)   # t0=0.65
        # midpoint between nodes: linear interpolation
        self.assertAlmostEqual(res.velocity_mps[225], 2250.0)   # t0=0.45


class TestSyntheticLinear(unittest.TestCase):
    """Hand-computed interpolation on a linear-amplitude trace."""

    def test_linear_trace_exact_hand_values(self):
        dt, delay, ns = 0.004, 0.008, 11
        times = delay + dt * np.arange(ns)          # 0.008 .. 0.048
        samples = 3.0 * times - 1.0                 # linear in recorded time
        tr = make_trace(offset=30.0, delay_s=delay, dt_s=dt, samples=samples)
        plan = make_plan(stretch_limit=100.0)       # keep everything
        res = correct_cdp([tr], const_vel(v=2000.0), plan)
        t0 = plan.t0_grid
        tx = np.sqrt(t0**2 + (30.0 / 2000.0)**2)
        self.assertTrue(np.all(tx >= times[0]) and np.all(tx <= times[-1]))
        # t0 == 0 with offset != 0: infinite stretch, always muted
        self.assertEqual(res.reason[0, 0], Reason.STRETCH_MUTE)
        self.assertTrue(np.isnan(res.corrected[0, 0]))
        # linear interpolation of linear data is exact everywhere else
        np.testing.assert_allclose(res.corrected[0, 1:], 3.0 * tx[1:] - 1.0,
                                   rtol=1e-12, atol=1e-15)
        self.assertTrue(np.all(res.valid[0, 1:]))
        # one fully hand-computed sample: t0 = 0.02 -> tx = 0.025
        i = 10
        self.assertAlmostEqual(t0[i], 0.020)
        self.assertAlmostEqual(tx[i], 0.025)
        r = (0.025 - 0.008) / 0.004                 # 4.25
        self.assertAlmostEqual(r, 4.25)
        s4, s5 = 3.0 * 0.024 - 1.0, 3.0 * 0.028 - 1.0
        self.assertAlmostEqual(res.corrected[0, i], 0.75 * s4 + 0.25 * s5)
        self.assertAlmostEqual(res.corrected[0, i], -0.925)

    def test_zero_offset_preserves_timeline_on_common_support(self):
        dt, ns = 0.002, 21
        samples = np.sin(np.arange(ns) * 0.7)       # arbitrary content
        plan = make_plan()
        # delay = 0: output grid == recorded grid -> exact copy
        tr = make_trace(offset=0.0, delay_s=0.0, dt_s=dt, samples=samples)
        res = correct_cdp([tr], const_vel(), plan)
        np.testing.assert_allclose(res.corrected[0], samples, atol=1e-15)
        self.assertTrue(np.all(res.valid[0]))
        # delay = 6 ms: before delay -> OUT_OF_SUPPORT (missing, not 0);
        # on the common support the original timeline is preserved
        tr2 = make_trace(offset=0.0, delay_s=0.006, dt_s=dt, samples=samples)
        res2 = correct_cdp([tr2], const_vel(), plan)
        self.assertTrue(np.all(res2.reason[0, :3] == Reason.OUT_OF_SUPPORT))
        self.assertTrue(np.all(np.isnan(res2.corrected[0, :3])))
        np.testing.assert_allclose(res2.corrected[0, 3:], samples[:-3],
                                   atol=1e-15)

    def test_exact_sample_boundaries(self):
        samples = np.array([10.0, 20.0, 30.0, 40.0, 50.0])
        dt = 0.002
        amp, k, f = sample_at(samples, dt, 0.0, 0.0)
        self.assertEqual((amp, k, f), (10.0, 0, 0.0))
        amp, k, f = sample_at(samples, dt, 0.0, 4 * dt)   # exact last
        self.assertEqual((amp, k, f), (50.0, 4, 0.0))
        amp, k, f = sample_at(samples, dt, 0.0, 1.5 * dt)
        self.assertEqual((amp, k), (25.0, 1))
        self.assertAlmostEqual(f, 0.5)
        self.assertIsNone(sample_at(samples, dt, 0.0, 4 * dt + 1e-6))
        self.assertIsNone(sample_at(samples, dt, 0.0, -1e-6))
        # exact hit through the vectorised path as well
        tr = make_trace(offset=0.0, dt_s=dt, samples=samples)
        plan = make_plan(t0_step_s=dt, output_count=5)
        res = correct_cdp([tr], const_vel(), plan)
        np.testing.assert_allclose(res.corrected[0], samples, atol=1e-15)

    def test_delay_beyond_tx_is_out_of_support_not_index_zero(self):
        # sample index 0 sits at 20 ms, not at t=0
        tr = make_trace(offset=0.0, delay_s=0.020, dt_s=0.002,
                        samples=np.ones(30))
        plan = make_plan(output_count=5)            # t0 = 0 .. 0.008
        res = correct_cdp([tr], const_vel(), plan)
        self.assertTrue(np.all(res.reason[0] == Reason.OUT_OF_SUPPORT))
        self.assertTrue(np.all(np.isnan(res.corrected[0])))
        self.assertEqual(int(res.fold.sum()), 0)
        self.assertTrue(np.all(np.isnan(res.stack)))


class TestStretchAndStackRules(unittest.TestCase):
    def test_stretch_factor_definition(self):
        self.assertEqual(stretch_factor(0.0, 0.0, 0.0), 0.0)
        self.assertEqual(stretch_factor(5.0, 0.0, 300.0), float("inf"))
        self.assertAlmostEqual(stretch_factor(0.5, 0.4, 100.0), 0.25)
        self.assertFalse(is_stretch_muted(0.35, 0.35))   # equality kept
        self.assertFalse(is_stretch_muted(0.349999, 0.35))
        self.assertTrue(is_stretch_muted(0.350001, 0.35))
        self.assertTrue(is_stretch_muted(float("inf"), 0.35))

    def test_stretch_mute_around_limit(self):
        # constant v: stretch = sqrt(1 + (c/t0)^2) - 1, decreasing in t0
        v, offset = 2000.0, 700.0
        c = offset / v
        tr = make_trace(offset=offset, dt_s=0.002, ns=400)
        plan = make_plan(output_count=360, stretch_limit=0.35)
        res = correct_cdp([tr], const_vel(v=v), plan)
        t0 = plan.t0_grid
        expected_muted = (np.sqrt(t0**2 + c**2) / np.where(t0 == 0, 1.0, t0)
                          - 1.0) > 0.35
        expected_muted[0] = True                      # t0=0, offset != 0
        np.testing.assert_array_equal(
            res.reason[0] == Reason.STRETCH_MUTE, expected_muted)
        # boundary by hand: first kept sample has stretch <= limit
        kept = np.flatnonzero(~expected_muted)
        self.assertGreater(kept[0], 0)
        tx = np.sqrt(t0[kept[0]]**2 + c**2)
        self.assertLessEqual(tx / t0[kept[0]] - 1.0, 0.35)

    def test_fold_zero_means_missing_stack(self):
        tr = make_trace(offset=100.0, dt_s=0.002, ns=400)
        plan = make_plan(output_count=50)
        res = correct_cdp([tr], const_vel(), plan)
        self.assertEqual(int(res.fold[0]), 0)         # t0=0 muted (inf)
        self.assertTrue(np.isnan(res.stack[0]))
        self.assertEqual(float(res.stack_sum[0]), 0.0)
        self.assertTrue(np.isnan(res.corrected[0, 0]))

    def test_dead_trace_contributes_nothing(self):
        live = make_trace(file_index=0, offset=0.0, samples=np.ones(30))
        dead = make_trace(file_index=1, offset=0.0, live=False,
                          samples=5.0 * np.ones(30))
        plan = make_plan(output_count=20)
        res = correct_cdp([live, dead], const_vel(), plan)
        np.testing.assert_array_equal(res.fold, np.ones(20, dtype=np.int64))
        np.testing.assert_allclose(res.stack, np.ones(20))
        self.assertTrue(np.all(res.reason[1] == Reason.DEAD_TRACE))

    def test_independent_fold_stack_loop(self):
        rng = np.random.default_rng(7)
        traces = [
            make_trace(file_index=i, cdp=1, offset=off,
                       delay_s=d, samples=rng.normal(size=60))
            for i, (off, d) in enumerate(
                [(0.0, 0.0), (150.0, 0.0), (-150.0, 0.004), (400.0, 0.0)])
        ]
        plan = make_plan(output_count=40)
        res = correct_cdp(traces, const_vel(v=1800.0), plan)
        for i in range(40):
            vals = [float(res.corrected[j, i]) for j in range(4)
                    if res.valid[j, i]]
            self.assertEqual(len(vals), int(res.fold[i]))
            self.assertAlmostEqual(sum(vals), float(res.stack_sum[i]),
                                   places=12)
            if vals:
                self.assertAlmostEqual(sum(vals) / len(vals),
                                       float(res.stack[i]), places=12)
            else:
                self.assertTrue(np.isnan(res.stack[i]))


class TestDiagnostics(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _corrupted(self, offset: int, value: int) -> str:
        return corrupted_copy(offset, value, self._tmp.name)

    def test_unsupported_format_diagnosed(self):
        bad = self._corrupted(BIN_FMT, 2)  # 4-byte fixed point
        with self.assertRaises(UnsupportedFormatError) as cm:
            correct(bad, VELCSV, load_plan(PLANJSON))
        self.assertIn("format code 2", str(cm.exception))

    def test_trace_ns_contradicts_binary(self):
        bad = self._corrupted(TRACE0_HDR + TRACE_NS, 421)
        with self.assertRaises(HeaderContradictionError) as cm:
            correct(bad, VELCSV, load_plan(PLANJSON))
        self.assertIn("421", str(cm.exception))

    def test_trace_dt_contradicts_binary(self):
        bad = self._corrupted(TRACE0_HDR + TRACE_DT, 4000)
        with self.assertRaises(HeaderContradictionError) as cm:
            correct(bad, VELCSV, load_plan(PLANJSON))
        self.assertIn("4000", str(cm.exception))

    def test_not_a_segy_file(self):
        bad = str(Path(self._tmp.name) / "junk.sgy")
        Path(bad).write_bytes(b"not segy at all" * 10)
        with self.assertRaises(UnsupportedFormatError):
            correct(bad, VELCSV, load_plan(PLANJSON))

    def test_velocity_foldback_diagnosed(self):
        table = {1: VelocityFunction(cdp=1,
                                     t0_s=np.array([0.0, 0.4, 0.41, 0.8]),
                                     vrms_mps=np.array([2000., 2000.,
                                                        20000., 20000.]))}
        tr = make_trace(cdp=1, offset=900.0, dt_s=0.002, ns=400)
        plan = make_plan(output_count=360)
        with self.assertRaises(NonMonotonicMappingError):
            correct(plan=plan, traces=[tr], table=table)

    def test_velocity_table_validation(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "v.csv"
            p.write_text("cdp,t0_s,vrms_mps\n1,0,2000\n1,0,2100\n")
            with self.assertRaises(VelocityTableError):  # repeated node
                load_velocity(str(p))
            p.write_text("cdp,t0_s,vrms_mps\n1,0,2000\n1,0.5,-3\n")
            with self.assertRaises(VelocityTableError):  # non-positive v
                load_velocity(str(p))
            p.write_text("cdp,t0_s,vrms_mps\n1,0,2000\n")
            with self.assertRaises(VelocityTableError):  # single node
                load_velocity(str(p))

    def test_velocity_coverage_and_missing_cdp(self):
        plan = make_plan(output_count=360)  # t0 up to 0.718
        tr = make_trace(cdp=1, offset=0.0, dt_s=0.002, ns=400)
        short = {1: const_vel(cdp=1, v=2000.0, t0_max=0.5)}
        with self.assertRaises(VelocityTableError) as cm:
            correct(plan=plan, traces=[tr], table=short)
        self.assertIn("extrapolation", str(cm.exception))
        with self.assertRaises(VelocityTableError) as cm:
            correct(plan=plan, traces=[tr], table={2: const_vel(cdp=2)})
        self.assertIn("missing from the velocity table", str(cm.exception))

    def test_plan_validation(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "plan.json"
            base = json.loads(Path(PLANJSON).read_text())
            for mutate in (
                lambda b: b.update(output_t0_step_s=0),
                lambda b: b.update(output_count=-3),
                lambda b: b.update(interpolation="cubic spline"),
                lambda b: b.update(offset_unit="feet"),
            ):
                bad = dict(base)
                mutate(bad)
                p.write_text(json.dumps(bad))
                with self.assertRaises(PlanError):
                    load_plan(str(p))


class TestExportAndCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = load_plan(PLANJSON)
        cls.result = correct(SEGY, VELCSV, cls.plan)

    def test_export_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            written = export_all(self.result, d)
            z = np.load(Path(d) / "nmo_result.npz")
            res = self.result.cdps[101]
            np.testing.assert_array_equal(z["cdp101_corrected"],
                                          res.corrected)
            np.testing.assert_array_equal(z["cdp101_reason"], res.reason)
            np.testing.assert_array_equal(z["cdp101_fold"], res.fold)
            np.testing.assert_array_equal(z["t0_s"], res.t0_s)
            # stacks.csv: fold column matches, mean empty iff fold == 0
            with open(Path(d) / "stacks.csv") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 3 * 360)
            for row in rows[::137]:  # spot-check spread
                cdp, i = int(row["cdp"]), round(float(row["t0_s"]) / 0.002)
                r2 = self.result.cdps[cdp]
                self.assertEqual(int(row["fold"]), int(r2.fold[i]))
                if r2.fold[i] == 0:
                    self.assertEqual(row["stack_mean"], "")
                else:
                    self.assertAlmostEqual(float(row["stack_mean"]),
                                           float(r2.stack[i]), places=8)
            # traces.csv accounts for the dead trace
            with open(Path(d) / "traces.csv") as fh:
                trows = list(csv.DictReader(fh))
            dead = [r for r in trows if r["file_index"] == "5"]
            self.assertEqual(len(dead), 1)
            self.assertEqual(int(dead[0]["n_dead"]), 360)
            self.assertEqual(dead[0]["live"], "False")
            # corrected CSV: invalid cells are empty, not zero
            with open(Path(d) / "corrected_cdp101.csv") as fh:
                crows = list(csv.reader(fh))
            self.assertEqual(crows[0][0], "file_index")
            j14 = next(r for r in crows[1:] if r[0] == "14")
            self.assertEqual(j14[3 + 359], "")       # out of support
            self.assertEqual(j14[3 + 100], "")       # stretch muted
            self.assertNotEqual(j14[3 + 300], "")
            self.assertTrue(all(p for ps in written.values() for p in ps))

    def test_cli_run_and_explain(self):
        import io
        from contextlib import redirect_stdout
        from nmo.__main__ import main
        with tempfile.TemporaryDirectory() as d:
            with redirect_stdout(io.StringIO()):
                rc = main(["run", SEGY, VELCSV, PLANJSON, "--out", d,
                           "--explain", "101", "7", "180"])
            self.assertEqual(rc, 0)
            self.assertTrue((Path(d) / "nmo_result.npz").exists())
        with redirect_stdout(io.StringIO()):
            rc = main(["explain", SEGY, VELCSV, PLANJSON,
                       "--cdp", "103", "--trace", "16", "--t0-index", "250"])
            self.assertEqual(rc, 0)
            rc = main(["explain", SEGY, VELCSV, PLANJSON, "--json",
                       "--cdp", "102", "--trace", "5", "--t0-index", "10"])
            self.assertEqual(rc, 0)

    def test_cli_diagnostic_exit_code(self):
        import io
        from contextlib import redirect_stderr
        from nmo.__main__ import main
        with tempfile.TemporaryDirectory() as d:
            bad = corrupted_copy(BIN_FMT, 2, d)
            with redirect_stderr(io.StringIO()):
                rc = main(["run", bad, VELCSV, PLANJSON, "--out", "unused"])
            self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
