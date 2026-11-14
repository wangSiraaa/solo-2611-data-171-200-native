"""Pure unit tests (no database) for parsing, frame derivation, and functions."""

from __future__ import annotations

import json
import random
import unittest
from pathlib import Path

from window_lab.engine import Row, build_partitions, explain, resolved_frame
from window_lab.sqlparse import ParseError, parse_query

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "fixtures" / "schema.json").read_text())

SEED = [
    (1, "A", 1, 10),
    (2, "A", 1, 20),
    (3, "A", 2, None),
    (4, "A", 4, 7),
    (5, "A", None, 3),
    (6, "A", None, 8),
    (7, "B", 1, 5),
    (8, "B", 3, 6),
    (9, "B", 3, 9),
]


def make_rows(order=None):
    data = list(SEED)
    if order is not None:
        data = [data[i] for i in order]
    return [
        Row(
            event_id=eid,
            values={"event_id": eid, "account": acc, "tick": tick, "amount": amount},
            input_index=index,
        )
        for index, (eid, acc, tick, amount) in enumerate(data)
    ]


def explain_sql(sql: str, rows=None):
    query = parse_query({"query_id": "t", "sql": sql}, SCHEMA)
    return explain(query, rows if rows is not None else make_rows())


def values_by_event(evidence, expression):
    return {
        result["event_id"]: result["value"]
        for result in evidence["results"]
        if result["expression"] == expression
    }


class ParserTest(unittest.TestCase):
    def test_rejects_join(self):
        sql = (
            "SELECT event_id, SUM(amount) OVER (ORDER BY tick) s "
            "FROM window_events JOIN window_events b ON true"
        )
        with self.assertRaises(ParseError):
            parse_query({"query_id": "x", "sql": sql}, SCHEMA)

    def test_rejects_where(self):
        sql = (
            "SELECT event_id, SUM(amount) OVER (ORDER BY tick) s "
            "FROM window_events WHERE account = 'A'"
        )
        with self.assertRaises(ParseError):
            parse_query({"query_id": "x", "sql": sql}, SCHEMA)

    def test_rejects_group_by(self):
        sql = (
            "SELECT event_id, SUM(amount) OVER (ORDER BY tick) s "
            "FROM window_events GROUP BY event_id"
        )
        with self.assertRaises(ParseError):
            parse_query({"query_id": "x", "sql": sql}, SCHEMA)

    def test_rejects_exclude(self):
        sql = (
            "SELECT event_id, SUM(amount) OVER (ORDER BY tick ROWS BETWEEN "
            "1 PRECEDING AND CURRENT ROW EXCLUDE CURRENT ROW) s FROM window_events"
        )
        with self.assertRaises(ParseError):
            parse_query({"query_id": "x", "sql": sql}, SCHEMA)

    def test_rejects_unknown_function(self):
        sql = (
            "SELECT event_id, ROW_NUMBER() OVER (ORDER BY tick) r FROM window_events"
        )
        with self.assertRaises(ParseError):
            parse_query({"query_id": "x", "sql": sql}, SCHEMA)

    def test_rejects_multi_key_range_offset(self):
        sql = (
            "SELECT event_id, SUM(amount) OVER (ORDER BY tick, event_id RANGE "
            "BETWEEN 1 PRECEDING AND CURRENT ROW) s FROM window_events"
        )
        with self.assertRaises(ParseError):
            parse_query({"query_id": "x", "sql": sql}, SCHEMA)

    def test_rejects_groups_without_order(self):
        sql = (
            "SELECT event_id, SUM(amount) OVER (PARTITION BY account "
            "GROUPS BETWEEN 1 PRECEDING AND CURRENT ROW) s FROM window_events"
        )
        with self.assertRaises(ParseError):
            parse_query({"query_id": "x", "sql": sql}, SCHEMA)

    def test_rejects_invalid_frame_order(self):
        sql = (
            "SELECT event_id, SUM(amount) OVER (ORDER BY tick ROWS BETWEEN "
            "CURRENT ROW AND 1 PRECEDING) s FROM window_events"
        )
        with self.assertRaises(ParseError):
            parse_query({"query_id": "x", "sql": sql}, SCHEMA)

    def test_rejects_illegal_window_inheritance(self):
        sql = (
            "SELECT event_id, SUM(amount) OVER w s FROM window_events "
            "WINDOW w AS (PARTITION BY account ORDER BY tick ROWS BETWEEN "
            "1 PRECEDING AND CURRENT ROW)"
        )
        self.assertTrue(parse_query({"query_id": "x", "sql": sql}, SCHEMA))
        bad = (
            "SELECT event_id, SUM(amount) OVER w s FROM window_events "
            "WINDOW base AS (PARTITION BY account), "
            "w AS (base PARTITION BY account)"
        )
        with self.assertRaises(ParseError):
            parse_query({"query_id": "x", "sql": bad}, SCHEMA)

    def test_rejects_forward_window_reference(self):
        sql = (
            "SELECT event_id, SUM(amount) OVER w1 s FROM window_events "
            "WINDOW w1 AS (w2), w2 AS (ORDER BY tick)"
        )
        with self.assertRaises(ParseError):
            parse_query({"query_id": "x", "sql": sql}, SCHEMA)

    def test_accepts_named_inheritance(self):
        sql = (
            "SELECT event_id, SUM(amount) OVER w AS total FROM window_events "
            "WINDOW base AS (PARTITION BY account ORDER BY tick NULLS LAST), "
            "w AS (base ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)"
        )
        query = parse_query({"query_id": "x", "sql": sql}, SCHEMA)
        window = query.windows[0].window
        self.assertEqual(window.partition_by, ["account"])
        self.assertEqual([key.column for key in window.order_by], ["tick"])
        self.assertEqual(window.frame.mode, "ROWS")


class DefaultFrameTest(unittest.TestCase):
    def test_default_frame_with_order_is_range_unbounded_current(self):
        rows = make_rows()
        query = parse_query(
            {
                "query_id": "t",
                "sql": "SELECT event_id, SUM(amount) OVER (ORDER BY tick) s "
                "FROM window_events",
            },
            SCHEMA,
        )
        frame = resolved_frame(query.windows[0].window)
        self.assertEqual(frame.mode, "RANGE")
        self.assertFalse(frame.explicit)


class FrameModeDifferenceTest(unittest.TestCase):
    """ROWS, GROUPS and RANGE must not all be interpreted as the previous N rows."""

    ROWS_SQL = (
        "SELECT event_id, SUM(amount) OVER (PARTITION BY account ORDER BY tick "
        "NULLS LAST ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) total "
        "FROM window_events"
    )
    GROUPS_SQL = (
        "SELECT event_id, SUM(amount) OVER (PARTITION BY account ORDER BY tick "
        "NULLS LAST GROUPS BETWEEN 1 PRECEDING AND CURRENT ROW) total "
        "FROM window_events"
    )
    RANGE_SQL = (
        "SELECT event_id, SUM(amount) OVER (PARTITION BY account ORDER BY tick "
        "NULLS LAST RANGE BETWEEN 1 PRECEDING AND CURRENT ROW) total "
        "FROM window_events"
    )

    def test_modes_differ(self):
        rows = make_rows()
        rows_values = values_by_event(explain_sql(self.ROWS_SQL, rows), "total")
        groups_values = values_by_event(explain_sql(self.GROUPS_SQL, rows), "total")
        range_values = values_by_event(explain_sql(self.RANGE_SQL, rows), "total")
        self.assertNotEqual(rows_values, groups_values)
        self.assertNotEqual(groups_values, range_values)
        # Pinned values derived by hand in docs/method.md (also cross-checked
        # against PostgreSQL 16 in the integration suite).
        self.assertEqual(rows_values[4]["value"], 7)
        self.assertEqual(groups_values[4]["value"], 7)
        self.assertEqual(range_values[4]["value"], 7)  # tick in [3,4]: only row 4

    def test_rows_and_groups_and_range_on_manual_frame_modes(self):
        # Same shape as fixtures/frame_modes.sql, account A only, ordered.
        data = [
            (1, "A", 1, 10),
            (2, "A", 1, 20),
            (3, "A", 3, 30),
            (4, "A", 4, None),
            (5, "A", 4, 40),
            (6, "A", None, 60),
            (7, "A", 5, None),
            (8, "A", 5, 80),
            (9, "A", 5, 90),
        ]
        rows = [
            Row(
                event_id=eid,
                values={"event_id": eid, "account": acc, "tick": tick, "amount": amount},
                input_index=index,
            )
            for index, (eid, acc, tick, amount) in enumerate(data)
        ]
        rows_values = values_by_event(explain_sql(self.ROWS_SQL, rows), "total")
        groups_values = values_by_event(explain_sql(self.GROUPS_SQL, rows), "total")
        range_values = values_by_event(explain_sql(self.RANGE_SQL, rows), "total")

        # tick is unique per group except the repeated values, so ROWS is
        # ambiguous on those cases while GROUPS/RANGE are exact.
        self.assertEqual(groups_values[1]["value"], 30)
        self.assertEqual(groups_values[3]["value"], 60)
        self.assertEqual(groups_values[7]["value"], 210)
        self.assertEqual(range_values[3]["value"], 30)
        self.assertEqual(range_values[4]["value"], 70)
        self.assertEqual(range_values[7]["value"], 210)
        self.assertFalse(rows_values[7]["deterministic"])


class DeterminismTest(unittest.TestCase):
    def test_ambiguous_rows_reports_ranges(self):
        sql = (
            "SELECT event_id, SUM(amount) OVER (PARTITION BY account ORDER BY tick "
            "ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS total FROM window_events"
        )
        evidence = explain_sql(sql)
        by_event = values_by_event(evidence, "total")
        # Rows in the tick=1 peer group can take either positional slot, so the
        # aggregate is not determined by the declared ordering.
        self.assertFalse(by_event[1]["deterministic"])
        self.assertFalse(by_event[2]["deterministic"])
        self.assertEqual(by_event[1]["possible_min"], 10)
        self.assertEqual(by_event[1]["possible_max"], 30)
        # The tick=2 row is alone in its peer group; the frame reaches back one
        # position into the tick=1 group, which may supply 10 or 20. The row
        # itself is NULL, so SUM is 10 or 20.
        self.assertFalse(by_event[3]["deterministic"])
        self.assertEqual(by_event[3]["possible_min"], 10)
        self.assertEqual(by_event[3]["possible_max"], 20)
        # The lone tick=4 row is fully determined: previous position is the
        # tick=2 row, current row is itself.
        self.assertTrue(by_event[4]["deterministic"])
        self.assertEqual(by_event[4]["value"], 7)

    def test_deterministic_values_ignore_input_order(self):
        sql = (
            "SELECT event_id, SUM(amount) OVER w AS total, COUNT(*) OVER w AS n, "
            "LAST_VALUE(amount) OVER w AS last_amount FROM window_events "
            "WINDOW w AS (PARTITION BY account ORDER BY tick NULLS LAST, event_id "
            "ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)"
        )
        baseline = explain_sql(sql, make_rows())
        rng = random.Random(173)
        base_values = {
            (result["event_id"], result["expression"]): result["value"]
            for result in baseline["results"]
            if result["value"]["deterministic"]
        }
        for _ in range(10):
            order = list(range(len(SEED)))
            rng.shuffle(order)
            shuffled = explain_sql(sql, make_rows(order))
            shuffled_values = {
                (result["event_id"], result["expression"]): result["value"]
                for result in shuffled["results"]
                if result["value"]["deterministic"]
            }
            self.assertEqual(base_values, shuffled_values)

    def test_nondeterministic_flags_ignore_input_order(self):
        sql = (
            "SELECT event_id, SUM(amount) OVER (PARTITION BY account ORDER BY tick "
            "ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS total FROM window_events"
        )
        baseline = explain_sql(sql)
        base_flags = sorted(
            (result["event_id"], result["value"]["deterministic"])
            for result in baseline["results"]
        )
        rng = random.Random(99)
        for _ in range(10):
            order = list(range(len(SEED)))
            rng.shuffle(order)
            shuffled = explain_sql(sql, make_rows(order))
            flags = sorted(
                (result["event_id"], result["value"]["deterministic"])
                for result in shuffled["results"]
            )
            self.assertEqual(base_flags, flags)

    def test_peer_groups_use_all_order_keys(self):
        # With event_id as a second key every row is its own peer group.
        query = parse_query(
            {
                "query_id": "t",
                "sql": "SELECT event_id, COUNT(*) OVER (ORDER BY tick NULLS LAST, "
                "event_id) n FROM window_events",
            },
            SCHEMA,
        )
        partitions = build_partitions(query.windows[0].window, make_rows())
        sizes = sorted(
            group.size for partition in partitions for group in partition.groups
        )
        self.assertEqual(sizes, [1] * len(SEED))

    def test_nulls_are_peers(self):
        query = parse_query(
            {
                "query_id": "t",
                "sql": "SELECT event_id, COUNT(*) OVER (ORDER BY tick) n "
                "FROM window_events",
            },
            SCHEMA,
        )
        partitions = build_partitions(query.windows[0].window, make_rows())
        null_group_sizes = [
            group.size
            for partition in partitions
            for group in partition.groups
            if group.order_values.get("tick") is None
        ]
        self.assertEqual(sorted(null_group_sizes), [2])


class FunctionTest(unittest.TestCase):
    def test_sum_ignores_null_but_all_null_is_null(self):
        sql = (
            "SELECT event_id, SUM(amount) OVER w AS total FROM window_events "
            "WINDOW w AS (PARTITION BY account ORDER BY event_id ROWS BETWEEN "
            "CURRENT ROW AND CURRENT ROW)"
        )
        evidence = explain_sql(sql)
        by_event = values_by_event(evidence, "total")
        self.assertEqual(by_event[3]["value"], None)

    def test_count_star_differs_from_count_column(self):
        sql = (
            "SELECT event_id, COUNT(*) OVER w AS n_star, COUNT(amount) OVER w AS n_amount "
            "FROM window_events WINDOW w AS (PARTITION BY account ORDER BY event_id "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
        )
        evidence = explain_sql(sql)
        stars = values_by_event(evidence, "n_star")
        amounts = values_by_event(evidence, "n_amount")
        # Account A: row 3 has NULL amount.
        self.assertEqual(stars[3]["value"], 3)
        self.assertEqual(amounts[3]["value"], 2)

    def test_empty_frame_semantics(self):
        sql = (
            "SELECT event_id, SUM(amount) OVER w AS total, COUNT(*) OVER w AS n "
            "FROM window_events WINDOW w AS (PARTITION BY account ORDER BY event_id "
            "ROWS BETWEEN 2 FOLLOWING AND 3 FOLLOWING)"
        )
        evidence = explain_sql(sql)
        totals = values_by_event(evidence, "total")
        counts = values_by_event(evidence, "n")
        self.assertIsNone(totals[6]["value"])
        self.assertEqual(counts[6]["value"], 0)

    def test_first_and_last_use_frame_boundaries(self):
        sql = (
            "SELECT event_id, FIRST_VALUE(amount) OVER w AS f, "
            "LAST_VALUE(amount) OVER w AS l FROM window_events "
            "WINDOW w AS (PARTITION BY account ORDER BY event_id "
            "ROWS BETWEEN 1 FOLLOWING AND 2 FOLLOWING)"
        )
        evidence = explain_sql(sql)
        firsts = values_by_event(evidence, "f")
        lasts = values_by_event(evidence, "l")
        account_a = [1, 2, 3, 4, 5, 6]
        for index, event_id in enumerate(account_a[:-1]):
            following = account_a[index + 1]
            self.assertEqual(firsts[event_id]["value"], SEED[following - 1][3])
        # Row 5 reaches 2 rows past its peer group (row 6 and nothing after);
        # row 6 has no rows following it -> empty frame -> NULL.
        self.assertEqual(lasts[5]["value"], 8)
        self.assertIsNone(lasts[6]["value"])

if __name__ == "__main__":
    unittest.main()
