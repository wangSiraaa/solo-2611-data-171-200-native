"""Integration tests against a dedicated PostgreSQL 16 test database.

They are skipped automatically when ``WINDOW_LAB_DSN`` is not set.  The suite
loads the fixture and manual frame tables, then:

* checks every deterministic cell against PostgreSQL,
* checks nondeterministic declarations are consistent with PostgreSQL,
* re-inserts rows in a different physical order and confirms determined
  results/flags do not change.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from window_lab.db import connect, execute_sql, load_rows
from window_lab.engine import explain
from window_lab.sqlparse import parse_query
from window_lab.verify import verify

ROOT = Path(__file__).resolve().parent.parent
DSN = os.environ.get("WINDOW_LAB_DSN", "")


def load_json(name: str) -> object:
    return json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))


@unittest.skipUnless(DSN, "WINDOW_LAB_DSN is not set")
class FixtureVerificationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = load_json("schema.json")
        cls.queries = load_json("queries.json")
        connection = connect(DSN)
        try:
            execute_sql(connection, "DROP TABLE IF EXISTS window_events")
            execute_sql(connection, (ROOT / "fixtures" / "seed.sql").read_text())
        finally:
            connection.close()

    def _verify(self, query_id: str) -> dict:
        spec = next(q for q in self.queries if q["query_id"] == query_id)
        query = parse_query(spec, self.schema)
        connection = connect(DSN)
        try:
            rows = load_rows(connection, query.table)
        finally:
            connection.close()
        return verify(query, rows, DSN)

    def test_every_fixture_query_verifies(self):
        for spec in self.queries:
            with self.subTest(query_id=spec["query_id"]):
                report = self._verify(spec["query_id"])
                self.assertTrue(report["verified"])
                self.assertEqual(report["summary"]["mismatch"], 0)
                self.assertTrue(report["data_consistent"])

    def test_ambiguous_rows_reports_nondeterminism(self):
        report = self._verify("ambiguous_rows")
        # Every nondeterministic cell must be within the declared possibilities.
        self.assertGreater(report["summary"]["nondeterministic"], 0)
        for check in report["checks"]:
            if check["status"] == "nondeterministic":
                self.assertIn("observed", check)


@unittest.skipUnless(DSN, "WINDOW_LAB_DSN is not set")
class InsertionOrderInvarianceTest(unittest.TestCase):
    """Changing physical insertion order must not change determined results."""

    @classmethod
    def setUpClass(cls):
        cls.schema = load_json("schema.json")
        cls.queries = load_json("queries.json")

    def _insert_permutation(self, insertion_order):
        data = [
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
        shuffled = [data[i] for i in insertion_order]
        connection = connect(DSN)
        try:
            with connection.cursor() as cursor:
                cursor.execute("DROP TABLE IF EXISTS window_events_perm")
                cursor.execute(
                    "CREATE TABLE window_events_perm"
                    "(event_id integer PRIMARY KEY, account text NOT NULL, "
                    "tick integer, amount integer)"
                )
                placeholders = ",".join(["%s"] * 4)
                for record in shuffled:
                    cursor.execute(
                        f"INSERT INTO window_events_perm VALUES ({placeholders})", record
                    )
        finally:
            connection.close()

    def _rows_in_insertion_order(self, insertion_order):
        from window_lab.engine import Row

        self._insert_permutation(insertion_order)
        connection = connect(DSN)
        try:
            rows = []
            with connection.cursor() as cursor:
                # Read in physical insertion order so the engine really sees
                # the permuted input, not a pre-sorted copy.
                cursor.execute("SELECT * FROM window_events_perm")
                for index, record in enumerate(cursor.fetchall()):
                    values = {
                        "event_id": record[0],
                        "account": record[1],
                        "tick": record[2],
                        "amount": record[3],
                    }
                    rows.append(Row(event_id=record[0], values=values, input_index=index))
            return rows
        finally:
            connection.close()

    def _signature(self, evidence):
        deterministic = sorted(
            (r["event_id"], r["expression"], json.dumps(r["value"].get("value")))
            for r in evidence["results"]
            if r["value"]["deterministic"]
        )
        flags = sorted(
            (r["event_id"], r["expression"], r["value"]["deterministic"])
            for r in evidence["results"]
        )
        return deterministic, flags

    def test_permutations_do_not_change_determined_results(self):
        orders = [
            list(range(9)),
            [8, 7, 6, 5, 4, 3, 2, 1, 0],
            [3, 0, 5, 1, 8, 2, 6, 4, 7],
            [5, 2, 7, 0, 4, 8, 1, 6, 3],
        ]
        for spec in self.queries:
            query = parse_query(spec, self.schema)
            signatures = []
            for order in orders:
                rows = self._rows_in_insertion_order(order)
                signatures.append(self._signature(explain(query, rows)))
            with self.subTest(query_id=spec["query_id"]):
                for signature in signatures[1:]:
                    self.assertEqual(signatures[0], signature)

    def test_permutations_match_postgres(self):
        perm_schema = dict(self.schema)
        perm_schema["table"] = "window_events_perm"
        for spec in self.queries:
            sql = spec["sql"].replace("window_events", "window_events_perm")
            perm_spec = {"query_id": spec["query_id"], "sql": sql}
            query = parse_query(perm_spec, perm_schema)
            for order in ([8, 7, 6, 5, 4, 3, 2, 1, 0], [3, 0, 5, 1, 8, 2, 6, 4, 7]):
                self._insert_permutation(order)
                report = verify(query, self._rows_in_insertion_order(order), DSN)
                with self.subTest(query_id=spec["query_id"], order=order):
                    self.assertEqual(report["summary"]["mismatch"], 0)


@unittest.skipUnless(DSN, "WINDOW_LAB_DSN is not set")
class ManualFrameTableTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = load_json("frame_modes_schema.json")
        cls.queries = load_json("frame_modes_queries.json")
        cls.init_sql = (ROOT / "fixtures" / "frame_modes.sql").read_text()
        connection = connect(DSN)
        try:
            execute_sql(connection, cls.init_sql)
        finally:
            connection.close()

    def test_manual_frame_queries_verify(self):
        for spec in self.queries:
            with self.subTest(query_id=spec["query_id"]):
                query = parse_query(spec, self.schema)
                connection = connect(DSN)
                try:
                    rows = load_rows(connection, query.table)
                finally:
                    connection.close()
                report = verify(query, rows, DSN)
                self.assertTrue(report["verified"])
                self.assertEqual(report["summary"]["mismatch"], 0)


if __name__ == "__main__":
    unittest.main()
