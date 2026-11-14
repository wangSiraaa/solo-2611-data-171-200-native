"""Test suite for the metric query planner.

Run:  python3 -m unittest discover -s tests -v
or:   python3 -m pytest tests -q
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from planner.api import Planner
from planner.errors import (  # noqa: E402
    CardinalityError, PlannerError, Unplannable, ValidationError,
)
from planner.loader import build_database, open_database  # noqa: E402
from planner.model import Model  # noqa: E402
from planner.planner import bind_query, explain_text  # noqa: E402
from planner.output import flatten_rows  # noqa: E402

import verify_independent  # noqa: E402

SQL = ROOT / "fixtures" / "schema_and_data.sql"
MODEL = ROOT / "fixtures" / "model.json"
EXT_MODEL = ROOT / "examples" / "model_extended.json"
QDIR = ROOT / "fixtures" / "queries"


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "task.db"
        build_database(SQL, cls.db_path)
        cls.model = Model.load(MODEL)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def conn(self):
        return open_database(self.db_path)

    def query(self, name):
        return json.loads((QDIR / name).read_text(encoding="utf-8"))

    def run_query(self, query, model=None):
        conn = self.conn()
        try:
            return Planner(model or self.model).run(conn, query)
        finally:
            conn.close()

    def rows_by_group(self, result):
        dims = result.bound.dims
        return {
            tuple(row["group"][d] for d in dims): row for row in result.rows
        }


class TestFixtures(Base):
    def test_region(self):
        result = self.run_query(self.query("region.json"))
        rows = self.rows_by_group(result)
        north = rows[("north",)]
        south = rows[("south",)]
        self.assertEqual(north["metrics"]["booked"], 18000)
        self.assertEqual(north["metrics"]["sales"], 18000)
        self.assertEqual(north["metrics"]["received"], 10000)
        self.assertEqual(north["metrics"]["order_count"], 3)
        self.assertAlmostEqual(north["metrics"]["paid_ratio"], 10000 / 18000)
        self.assertEqual(south["metrics"]["booked"], 20000)
        self.assertEqual(south["metrics"]["received"], 18000)
        self.assertAlmostEqual(south["metrics"]["paid_ratio"], 0.9)
        # support columns: numerator/denominator preserved
        self.assertEqual(north["base"]["received"], 10000)
        self.assertEqual(north["base"]["booked"], 18000)
        self.assertEqual(north["fact_rows"]["orders"], 3)
        self.assertEqual(north["fact_rows"]["lines"], 4)
        self.assertEqual(north["fact_rows"]["payments"], 3)

    def test_category_sales(self):
        result = self.run_query(self.query("category_sales.json"))
        rows = self.rows_by_group(result)
        self.assertEqual(rows[("A",)]["metrics"]["sales"], 19000)
        self.assertEqual(rows[("A",)]["metrics"]["units"], 4)
        self.assertEqual(rows[("B",)]["metrics"]["sales"], 19000)
        self.assertEqual(rows[("B",)]["metrics"]["units"], 5)

    def test_orders_with_a_entity_exists(self):
        """entity_exists(lines.category=A) restricts ALL metrics to A orders."""
        result = self.run_query(self.query("orders_with_a.json"))
        rows = self.rows_by_group(result)
        # A orders: O1(north),O2(north),O4(north),O5(south)
        self.assertEqual(rows[("north",)]["metrics"]["booked"], 18000)
        self.assertEqual(rows[("south",)]["metrics"]["booked"], 5000)
        self.assertEqual(rows[("south",)]["metrics"]["received"], 3000)
        # B-only order O3 is excluded everywhere
        self.assertEqual(rows[("south",)]["metrics"]["sales"], 5000)

    def test_local_a(self):
        """local(lines.category=A) only filters lines; payments are untouched."""
        result = self.run_query(self.query("local_a.json"))
        rows = self.rows_by_group(result)
        # lines A: L1 6000 (O1 north), L3 8000 (O2 north), L5 0 (O4 north), L6 5000 (O5 south)
        self.assertEqual(rows[("north",)]["metrics"]["sales"], 14000)
        self.assertEqual(rows[("south",)]["metrics"]["sales"], 5000)
        # payments unaffected: north 10000 (O1,O4), south 18000 (O3,O5)
        self.assertEqual(rows[("north",)]["metrics"]["received"], 10000)
        self.assertEqual(rows[("south",)]["metrics"]["received"], 18000)

    def test_local_vs_entity_exists_distinct(self):
        """The two scopes must produce different numbers for the same dimension."""
        local = self.rows_by_group(self.run_query(self.query("local_a.json")))[("south",)]
        entity = self.rows_by_group(
            self.run_query(self.query("orders_with_a.json")))[("south",)]
        # south sales: local=5000 (only A lines), entity_exists=5000 (only A orders)
        self.assertEqual(local["metrics"]["sales"], 5000)
        # but received differs: local keeps all south payments (18000),
        # entity_exists restricts to A orders (3000).
        self.assertEqual(local["metrics"]["received"], 18000)
        self.assertEqual(entity["metrics"]["received"], 3000)

    def test_combined(self):
        result = self.run_query(self.query("combined.json"))
        rows = self.rows_by_group(result)
        # entity_exists A + local payments.method=bank
        n1 = rows[("north", "2026-01-01")]
        self.assertEqual(n1["metrics"]["sales"], 10000)   # O1 lines (L1+L2)
        self.assertEqual(n1["metrics"]["received"], 6000) # O1 bank T2
        self.assertAlmostEqual(n1["metrics"]["paid_ratio"], 0.6)
        # day 2026-01-02: O2 is an A order (L3), has no bank payment -> 0
        n2 = rows[("north", "2026-01-02")]
        self.assertEqual(n2["metrics"]["sales"], 8000)
        self.assertEqual(n2["metrics"]["received"], 0)
        self.assertEqual(n2["metrics"]["paid_ratio"], 0.0)
        # day 2026-01-03: O4 has zero amounts and O5 no A line twice? south O5 is A.
        # north 2026-01-03 has only O4 (booked 0 => denominator 0 => null ratio)
        n3 = rows[("north", "2026-01-03")]
        self.assertEqual(n3["metrics"]["sales"], 0)
        self.assertEqual(n3["metrics"]["paid_ratio"], None)
        self.assertEqual(n3["base"]["booked"], 0)

    def test_invalid_category_payment_unplannable(self):
        with self.assertRaises(Unplannable) as ctx:
            self.run_query(self.query("invalid_category_payment.json"))
        self.assertIn("received", str(ctx.exception))
        self.assertIn("category", str(ctx.exception))
        self.assertTrue(ctx.exception.paths)
        path = ctx.exception.paths[0]
        self.assertEqual(path["metric"], "received")
        self.assertEqual(path["fact"], "payments")
        self.assertEqual(path["dimension"], "category")
        self.assertIn("no", path["reason"].lower())

    def test_no_hardcoded_report_names(self):
        """A brand-new metric + query combination must plan without code changes."""
        model = Model.load(EXT_MODEL)
        q = json.loads((ROOT / "examples" / "new_query.json").read_text())
        result = self.run_query(q, model=model)
        rows = self.rows_by_group(result)
        north = rows[("north",)]
        self.assertEqual(north["metrics"]["booked"], 10000)
        self.assertEqual(north["metrics"]["sales"], 6000)
        self.assertEqual(north["metrics"]["units"], 1)
        self.assertEqual(north["metrics"]["received"], 10000)
        self.assertEqual(north["metrics"]["payment_count"], 3)
        self.assertAlmostEqual(north["metrics"]["line_share_ratio"], 0.6)
        self.assertAlmostEqual(north["metrics"]["avg_line_value"], 6000.0)
        self.assertAlmostEqual(north["metrics"]["avg_payment_value"],
                               10000 / 3)

    def test_all_fixtures_match_independent_verifier(self):
        conn = self.conn()
        try:
            for qf in sorted(QDIR.glob("*.json")):
                if qf.name == "invalid_category_payment.json":
                    continue
                problems = verify_independent.compare(
                    conn, self.model, json.loads(qf.read_text(encoding="utf-8")))
                self.assertEqual(problems, [], msg=f"{qf.name}: {problems}")
        finally:
            conn.close()


class TestAntiFanout(Base):
    """Adding rows to one fact must not inflate another fact's metrics."""

    def test_extra_payment_does_not_inflate_amounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mut.db"
            build_database(SQL, db)
            c = sqlite3.connect(db)
            c.execute("INSERT INTO payments VALUES ('T99','O1','bank',7777)")
            c.execute("INSERT INTO lines VALUES ('L99','O1','P2',1,1234)")
            c.commit(); c.close()
            conn = open_database(db)
            try:
                before = self.rows_by_group(
                    Planner(self.model).run(conn, self.query("region.json")))
                conn.close()
            finally:
                pass
            # baseline numbers known from fixtures
            north_after = before[("north",)]
            self.assertEqual(north_after["metrics"]["booked"], 18000)
            self.assertEqual(north_after["metrics"]["sales"], 18000 + 1234)
            self.assertEqual(north_after["metrics"]["received"], 10000 + 7777)
            self.assertEqual(north_after["metrics"]["order_count"], 3)
            self.assertAlmostEqual(north_after["metrics"]["paid_ratio"],
                                   (10000 + 7777) / 18000)

    def test_independent_verifier_after_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mut2.db"
            build_database(SQL, db)
            c = sqlite3.connect(db)
            c.execute("INSERT INTO payments VALUES ('T98','O2','bank',999)")
            c.commit(); c.close()
            conn = open_database(db)
            try:
                for name in ("region.json", "combined.json",
                             "orders_with_a.json", "local_a.json"):
                    problems = verify_independent.compare(
                        conn, self.model, self.query(name))
                    self.assertEqual(problems, [], msg=f"{name}: {problems}")
            finally:
                conn.close()

    def test_entity_exists_filter_not_inflated_by_new_matching_line(self):
        """Adding a B line to an A order must not change entity_exists(A) metrics.

        The order still qualifies (it already had an A line), and bookkeeping
        per order is unchanged; sales only grows by the genuine line amount.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mut3.db"
            build_database(SQL, db)
            c = sqlite3.connect(db)
            # O1 already has A line L1; add a B line: makes O1 "more B"
            c.execute("INSERT INTO lines VALUES ('L97','O1','P2',5,5000)")
            c.commit(); c.close()
            conn = open_database(db)
            try:
                rows = self.rows_by_group(
                    Planner(self.model).run(conn, self.query("orders_with_a.json")))
                north = rows[("north",)]
                # booked unchanged: orders fact untouched
                self.assertEqual(north["metrics"]["booked"], 18000)
                # received unchanged: payments fact untouched
                self.assertEqual(north["metrics"]["received"], 10000)
                # sales grows only by the added line (all of O1's lines count
                # because the entity set is order-based)
                self.assertEqual(north["metrics"]["sales"], 18000 + 5000)
            finally:
                conn.close()

    def test_local_filter_not_propagated_to_payments(self):
        """local(lines) must not restrict payments, and payments must not inflate sales.

        A new payment legitimately changes `received` (payments are outside the
        lines-local filter), while `sales` stays fixed by the A-line filter.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mut4.db"
            build_database(SQL, db)
            c = sqlite3.connect(db)
            c.execute("INSERT INTO payments VALUES ('T96','O1','card',2222)")
            c.commit(); c.close()
            conn = open_database(db)
            try:
                rows = self.rows_by_group(
                    Planner(self.model).run(conn, self.query("local_a.json")))
                # sales unchanged (A lines only); the added payment does not
                # inflate it, which is the fan-out guard we care about
                self.assertEqual(rows[("north",)]["metrics"]["sales"], 14000)
                # received grows: a lines-local filter must NOT propagate onto
                # payments. 10000 + 2222.
                self.assertEqual(rows[("north",)]["metrics"]["received"], 12222)
            finally:
                conn.close()

    def test_combined_scopes_survive_unrelated_mutation(self):
        """Mutate a row that no scope selects; combined output stays identical."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mut5.db"
            build_database(SQL, db)
            # snapshot before
            conn = open_database(db)
            before = self.rows_by_group(
                Planner(self.model).run(conn, self.query("combined.json")))
            conn.close()
            c = sqlite3.connect(db)
            # O3 is excluded by entity_exists(A); a card payment on O3 is
            # irrelevant to local(method=bank) and to the A order set.
            c.execute("INSERT INTO payments VALUES ('T95','O3','card',12345)")
            c.commit(); c.close()
            conn = open_database(db)
            after = self.rows_by_group(
                Planner(self.model).run(conn, self.query("combined.json")))
            conn.close()
            for key in before:
                for metric in before[key]["metrics"]:
                    b = before[key]["metrics"][metric]
                    a = after[key]["metrics"][metric]
                    if isinstance(b, float) or isinstance(a, float):
                        self.assertAlmostEqual(b, a, msg=f"{key}/{metric}")
                    else:
                        self.assertEqual(b, a, msg=f"{key}/{metric}")


class TestSemantics(Base):
    def test_zero_denominator_is_null(self):
        # combined: north 2026-01-03 contains only O4 (booked 0), so the
        # denominator is 0 and the model's zero_denominator (null) applies.
        result = self.run_query(self.query("combined.json"))
        rows = self.rows_by_group(result)
        n3 = rows[("north", "2026-01-03")]
        self.assertEqual(n3["base"]["booked"], 0)
        self.assertEqual(n3["metrics"]["paid_ratio"], None)

    def test_empty_measure_for_absent_fact_group(self):
        """Groups present in one fact but not another get empty_measure=0."""
        q = {"dimensions": ["day"], "metrics": ["booked", "received"],
             "filters": []}
        result = self.run_query(q)
        rows = self.rows_by_group(result)
        # 2026-01-02: order O2 exists (booked 8000) but no payment (received 0)
        self.assertEqual(rows[("2026-01-02",)]["metrics"]["booked"], 8000)
        self.assertEqual(rows[("2026-01-02",)]["metrics"]["received"], 0)
        self.assertEqual(rows[("2026-01-02",)]["fact_rows"]["payments"], 0)

    def test_group_union_domain(self):
        q = {"dimensions": ["category"], "metrics": ["sales", "units"],
             "filters": []}
        result = self.run_query(q)
        keys = set(self.rows_by_group(result))
        self.assertEqual(keys, {("A",), ("B",)})

    def test_partial_fact_contribution_group_is_kept(self):
        """A group present only via payments still appears, with 0 from others.

        Dimension `method` is reachable only from payments, so group by method
        using only payment-sourced metrics plus a payments-local filter that
        matches nothing in one branch.  Here every method group comes only from
        payments; orders/lines contribute 0 by the empty_measure rule.
        """
        q = {"dimensions": ["method"],
             "metrics": ["payment_count", "received"], "filters": []}
        result = self.run_query(q)
        rows = self.rows_by_group(result)
        self.assertEqual(rows[("card",)]["metrics"]["payment_count"], 3)
        self.assertEqual(rows[("card",)]["metrics"]["received"], 14000)
        self.assertEqual(rows[("bank",)]["metrics"]["payment_count"], 3)
        self.assertEqual(rows[("bank",)]["metrics"]["received"], 14000)

    def test_absent_all_metrics_omitted(self):
        """Groups that no fact contributes to never appear (union domain)."""
        q = {"dimensions": ["region"], "metrics": ["sales", "received"],
             "filters": [{"scope": "entity_exists", "fact": "lines",
                          "dimension": "category", "op": "eq", "value": "Z"}]}
        result = self.run_query(q)
        self.assertEqual(result.rows, [])

    def test_ungrouped_totals(self):
        q = {"dimensions": [], "metrics": ["booked", "sales", "received",
                                           "paid_ratio"], "filters": []}
        result = self.run_query(q)
        self.assertEqual(len(result.rows), 1)
        row = result.rows[0]
        self.assertEqual(row["metrics"]["booked"], 38000)
        self.assertEqual(row["metrics"]["sales"], 38000)
        self.assertEqual(row["metrics"]["received"], 28000)
        self.assertAlmostEqual(row["metrics"]["paid_ratio"], 28000 / 38000)

    def test_empty_group_after_filter(self):
        """A local filter that matches no orders yields one group with 0/empty."""
        q = {"dimensions": ["region"], "metrics": ["booked", "sales"],
             "filters": [{"scope": "local", "fact": "orders",
                          "dimension": "day", "op": "eq",
                          "value": "1999-01-01"}]}
        result = self.run_query(q)
        rows = self.rows_by_group(result)
        # orders contributes nothing, but lines still has all its groups, so
        # region groups survive with booked = empty_measure (0)
        self.assertEqual(rows[("north",)]["metrics"]["booked"], 0)
        self.assertEqual(rows[("north",)]["metrics"]["sales"], 18000)

    def test_ungrouped_empty_result(self):
        q = {"dimensions": [], "metrics": ["booked"],
             "filters": [{"scope": "local", "fact": "orders",
                          "dimension": "day", "op": "eq",
                          "value": "1999-01-01"}]}
        result = self.run_query(q)
        # ungrouped aggregate over zero rows must not invent a row
        self.assertEqual(result.rows, [])

    def test_support_columns_always_present(self):
        result = self.run_query(self.query("combined.json"))
        cols, rows = flatten_rows(result)
        self.assertIn("paid_ratio__numerator", cols)
        self.assertIn("paid_ratio__denominator", cols)
        self.assertIn("__rows_orders", cols)
        self.assertIn("__rows_lines", cols)
        self.assertIn("__rows_payments", cols)


class TestValidation(Base):
    def test_unknown_dimension(self):
        with self.assertRaises(ValidationError):
            self.run_query({"dimensions": ["nope"], "metrics": ["sales"],
                            "filters": []})

    def test_unknown_metric(self):
        with self.assertRaises(ValidationError):
            self.run_query({"dimensions": [], "metrics": ["ghost"],
                            "filters": []})

    def test_unknown_fact_in_filter(self):
        with self.assertRaises(ValidationError):
            self.run_query({"dimensions": ["region"], "metrics": ["sales"],
                            "filters": [{"scope": "local", "fact": "ghost",
                                         "dimension": "region", "op": "eq",
                                         "value": "north"}]})

    def test_unreachable_filter_fact_dimension(self):
        with self.assertRaises(ValidationError) as ctx:
            self.run_query({"dimensions": ["region"], "metrics": ["received"],
                            "filters": [{"scope": "local", "fact": "payments",
                                         "dimension": "category", "op": "eq",
                                         "value": "A"}]})
        self.assertIn("category", str(ctx.exception))

    def test_bad_op(self):
        with self.assertRaises(ValidationError):
            self.run_query({"dimensions": ["region"], "metrics": ["sales"],
                            "filters": [{"scope": "local", "fact": "lines",
                                         "dimension": "category", "op": "like",
                                         "value": "A%"}]})

    def test_ratio_with_unreachable_denominator(self):
        """line_share_ratio denominator `booked` cannot be grouped by category."""
        model = Model.load(EXT_MODEL)
        with self.assertRaises(Unplannable) as ctx:
            self.run_query({"dimensions": ["category"],
                            "metrics": ["avg_line_value", "line_share_ratio"],
                            "filters": []}, model=model)
        self.assertIn("booked", str(ctx.exception))

    def test_inert_local_filter_is_reported(self):
        q = {"dimensions": ["region"], "metrics": ["booked"],
             "filters": [{"scope": "local", "fact": "lines",
                          "dimension": "category", "op": "eq", "value": "A"}]}
        conn = self.conn()
        try:
            result = Planner(self.model).plan_only(conn, q)
        finally:
            conn.close()
        inert = result.plan["inert_local_filters"]
        self.assertEqual(len(inert), 1)
        self.assertIn("lines", inert[0]["filter"])
        rows = self.rows_by_group(self.run_query(q))
        # booked must equal the unfiltered totals: the local filter is inert
        self.assertEqual(rows[("north",)]["metrics"]["booked"], 18000)
        self.assertEqual(rows[("south",)]["metrics"]["booked"], 20000)

    def test_method_sales_unplannable(self):
        with self.assertRaises(Unplannable):
            self.run_query({"dimensions": ["method"], "metrics": ["sales"],
                            "filters": []})


class TestCardinality(Base):
    def test_duplicate_destination_key_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bad.db"
            build_database(SQL, db)
            c = sqlite3.connect(db)
            c.execute("PRAGMA foreign_keys=OFF")
            patch = (ROOT / "fixtures" / "bad_cardinality_patch.sql").read_text()
            c.executescript(patch)
            c.commit(); c.close()
            conn = open_database(db)
            try:
                with self.assertRaises(CardinalityError) as ctx:
                    Planner(self.model).run(conn, self.query("region.json"))
                err = ctx.exception
                self.assertTrue(err.violations)
                v = err.violations[0]
                self.assertEqual(v["key"], "C1")
                self.assertEqual(v["count"], 2)
                self.assertIn("customer_id", v["relation"])
            finally:
                conn.close()

    def test_cardinality_checked_before_aggregation(self):
        """verify must fail too, independent of any aggregation."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bad2.db"
            build_database(SQL, db)
            c = sqlite3.connect(db)
            c.execute("PRAGMA foreign_keys=OFF")
            c.executescript(
                (ROOT / "fixtures" / "bad_cardinality_patch.sql").read_text())
            c.commit(); c.close()
            conn = open_database(db)
            try:
                with self.assertRaises(CardinalityError):
                    self.model.validate_against_db(conn)
            finally:
                conn.close()

    def test_orphan_entity_key_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "orphan.db"
            build_database(SQL, db)
            c = sqlite3.connect(db)
            c.execute("PRAGMA foreign_keys=OFF")
            c.execute("INSERT INTO payments VALUES ('T90','OZZ','card',100)")
            c.commit(); c.close()
            conn = open_database(db)
            try:
                with self.assertRaises(CardinalityError) as ctx:
                    self.model.validate_against_db(conn)
                self.assertEqual(ctx.exception.violations[0]["key"], "OZZ")
            finally:
                conn.close()


class TestPlanAndExplain(Base):
    def test_plan_contains_required_fields(self):
        conn = self.conn()
        try:
            result = Planner(self.model).plan_only(conn, self.query("combined.json"))
        finally:
            conn.close()
        plan = result.plan
        self.assertIn("tasks", plan)
        self.assertIn("combination", plan)
        facts = {t["fact"] for t in plan["tasks"]}
        self.assertEqual(facts, {"orders", "lines", "payments"})
        for t in plan["tasks"]:
            self.assertIn("grain", t)
            self.assertIn("local_filters", t)
            self.assertIn("entity_exists_restriction", t)
            self.assertIn("sql", t)
        self.assertEqual(plan["combination"]["group_key"], ["region", "day"])
        self.assertTrue(plan["why_no_double_count"])

    def test_explain_text_renders(self):
        conn = self.conn()
        try:
            result = Planner(self.model).plan_only(conn, self.query("combined.json"))
        finally:
            conn.close()
        text = explain_text(self.model, result.bound, result.plan)
        self.assertIn("事实 Task: orders", text)
        self.assertIn("事实 Task: lines", text)
        self.assertIn("事实 Task: payments", text)
        self.assertIn("为什么不会重复计量", text)

    def test_each_fact_is_separate_statement(self):
        """Each aggregation statement reads exactly one fact as its base table.

        Other fact tables may appear only as many_to_one dimension lookups
        (e.g. lines -> orders to read order_date); they must never be the
        aggregated table and never be summed/counted.  That is the property
        that prevents fan-out.
        """
        import re

        conn = self.conn()
        try:
            result = Planner(self.model).plan_only(conn, self.query("combined.json"))
        finally:
            conn.close()
        model = self.model
        for task in result.plan["tasks"]:
            sql = task["sql"]
            self.assertEqual(sql.count(" AS t0"), 1, msg=task["fact"])
            self.assertIn(f'FROM "{task["fact"]}" AS t0', sql)
            # every aggregate must be over t0 columns only
            for expr in re.findall(r"(?:SUM|COUNT)\((.*?)\)", sql):
                if expr.strip() == "*":
                    continue
                self.assertTrue(expr.startswith("t0."),
                                msg=f"{task['fact']} aggregates non-base table: {expr}")
            # any join into another fact must be on its unique key column
            for other in model.facts:
                if other == task["fact"]:
                    continue
                for m in re.finditer(rf'JOIN "{other}" AS (j\d+)', sql):
                    alias = m.group(1)
                    fact = model.facts[other]
                    self.assertIn(f'{alias}."{fact.key[0]}"', sql,
                                  msg=f"join to {other} not on unique key")

    def test_union_group_key_never_duplicates(self):
        q = {"dimensions": ["region"], "metrics": ["booked", "sales",
                                                    "received"], "filters": []}
        result = self.run_query(q)
        keys = [tuple(r["group"][d] for d in result.bound.dims)
                for r in result.rows]
        self.assertEqual(len(keys), len(set(keys)))


class TestExtendedModel(Base):
    def test_extended_model_still_correct(self):
        model = Model.load(EXT_MODEL)
        conn = self.conn()
        try:
            problems = verify_independent.compare(
                conn, model,
                json.loads((ROOT / "examples" / "new_query.json").read_text()))
        finally:
            conn.close()
        self.assertEqual(problems, [])

    def test_ratio_nesting_rejected(self):
        raw = json.loads(EXT_MODEL.read_text())
        raw["metrics"]["nested"] = {
            "op": "ratio", "numerator": "paid_ratio", "denominator": "booked"}
        with self.assertRaises(ValidationError):
            Model.from_dict(raw)


if __name__ == "__main__":
    unittest.main()
