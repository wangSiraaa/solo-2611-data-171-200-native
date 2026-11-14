#!/usr/bin/env python3
"""Regression tests for the stratified Datalog evaluator.

Run with ``python3 -m unittest discover -s tests -v`` from the repository
root.  Only the Python 3.11 standard library is used.
"""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path

import datalog

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def build(schema_file, rules_file):
    issues: list[datalog.Issue] = []
    schema = datalog.parse_schema(load(schema_file), issues)
    rules = datalog.parse_rules(load(rules_file), schema, issues)
    program = datalog.build_program(schema, rules, issues)
    return schema, rules, program, issues


def parse_facts_text(text: str, schema) -> tuple:
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
        handle.write(text)
        path = Path(handle.name)
    try:
        return datalog.parse_facts(path, schema, [])
    finally:
        path.unlink(missing_ok=True)


def run_fixture(schema_file="schema.json", rules_file="rules.json", facts_file="facts.jsonl"):
    schema, rules, program, issues = build(schema_file, rules_file)
    assert program is not None, [i.to_json() for i in issues]
    issues2: list[datalog.Issue] = []
    facts, counts, duplicates = datalog.parse_facts(FIXTURES / facts_file, schema, issues2)
    assert not issues2, [i.to_json() for i in issues2]
    evaluation = datalog.evaluate(program, facts, counts, duplicates)
    return program, evaluation


class MainFixtureTests(unittest.TestCase):
    def setUp(self):
        self.program, self.evaluation = run_fixture()

    def relations(self, predicate):
        return sorted(self.evaluation.relations[predicate])

    def test_stratification(self):
        strata = self.program.strata
        self.assertEqual(strata[0].predicates, ("banned", "link", "supplier", "suspended"))
        self.assertEqual(strata[1].predicates, ("reach",))
        self.assertEqual(
            self.program.predicate_stratum["reach"], 1
        )
        # allowed depends negatively on banned, so banned is in a lower stratum.
        self.assertLess(
            self.program.predicate_stratum["banned"],
            self.program.predicate_stratum["allowed"],
        )
        self.assertLess(
            self.program.predicate_stratum["eligible"],
            self.program.predicate_stratum["review"],
        )

    def test_duplicate_fact_removed(self):
        self.assertEqual(self.evaluation.duplicate_input_facts, 1)
        self.assertEqual(len(self.evaluation.input_relations["link"]), 7)

    def test_cyclic_reach_closure(self):
        expected = {
            ("A", "A"), ("A", "B"), ("A", "W"),
            ("B", "A"), ("B", "B"), ("B", "W"),
            ("C", "W"),
            ("S1", "A"), ("S1", "B"), ("S1", "W"),
            ("S2", "A"), ("S2", "B"), ("S2", "W"),
            ("S3", "C"), ("S3", "W"),
        }
        self.assertEqual(set(self.relations("reach")), expected)

    def test_cross_stratum_negation(self):
        # S1 reaches W but banned(S1, W) blocks allowed(S1, W).
        self.assertNotIn(("S1", "W"), self.relations("allowed"))
        # S3 is suspended, so even though the rule's positive part holds it
        # is not eligible; S4 never reaches W.
        self.assertEqual(self.relations("eligible"), [("S2",)])
        self.assertEqual(self.relations("review"), [("S1",), ("S3",), ("S4",)])

    def test_no_cyclic_proofs(self):
        result = datalog.verify_proofs(self.evaluation)
        self.assertTrue(result["ok"], result["problems"])
        # A fact reachable only through the cycle still has a finite proof.
        proof = self.evaluation.proofs[("reach", ("A", "A"))]
        self.assertEqual(proof.source, "rule")
        round_of = {
            premise: self.evaluation.proofs[premise].round
            for _, premise in proof.positive
        }
        self.assertTrue(all(r < proof.round for r in round_of.values()))

    def test_semi_naive_delta_variants(self):
        reach_result = next(
            s for s in self.evaluation.strata_results if s.index == 1
        )
        self.assertEqual([r.delta["reach"] for r in reach_result.rounds[:-1]], [7, 7, 1])
        self.assertEqual(reach_result.rounds[-1].delta, {})
        # Round 1 is the base rule; later rounds only join deltas of reach.
        self.assertNotIn("r2", reach_result.rounds[0].rules)
        for report in reach_result.rounds[1:]:
            for stat in report.rules.values():
                self.assertTrue(stat.delta_variants)

    def test_query_semantics(self):
        queries = [
            datalog.Atom("eligible", ("S2",)),
            datalog.Atom("eligible", ("S4",)),
        ]
        explained = datalog.explain_queries(self.evaluation, queries)
        by_fact = {entry["fact"]: entry for entry in explained["queries"]}
        self.assertEqual(by_fact["eligible(S2)"]["status"], "derived")
        self.assertEqual(by_fact["eligible(S4)"]["status"], "not_derived")
        self.assertTrue(by_fact["eligible(S4)"]["saturated"])
        self.assertNotIn("proof", by_fact["eligible(S4)"])

    def test_reordering_invariance(self):
        facts = [
            json.loads(line)
            for line in (FIXTURES / "facts.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        rules = load("rules.json")
        random.Random(1234).shuffle(facts)
        random.Random(1234).shuffle(rules)

        schema, parsed_rules, program, _ = build("schema.json", "rules.json")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            rng = random.Random(99)
            shuffled_rules = list(rules)
            rng.shuffle(shuffled_rules)
            (tmp / "rules.json").write_text(json.dumps(shuffled_rules), encoding="utf-8")
            shuffled_facts = list(facts)
            rng.shuffle(shuffled_facts)
            (tmp / "facts.jsonl").write_text(
                "\n".join(json.dumps(f) for f in shuffled_facts) + "\n", encoding="utf-8"
            )
            issues: list[datalog.Issue] = []
            schema2 = datalog.parse_schema(load("schema.json"), issues)
            rules2 = datalog.parse_rules(json.loads((tmp / "rules.json").read_text()), schema2, issues)
            program2 = datalog.build_program(schema2, rules2, issues)
            facts2, counts2, dup2 = datalog.parse_facts(tmp / "facts.jsonl", schema2, issues)
            evaluation2 = datalog.evaluate(program2, facts2, counts2, dup2)

        self.assertEqual(self.evaluation.relations, evaluation2.relations)
        self.assertEqual(self.evaluation.proofs, evaluation2.proofs)


class BadProgramTests(unittest.TestCase):
    def test_unsafe_rule_rejected(self):
        _, _, program, issues = build("schema.json", "bad-unsafe.json")
        self.assertIsNone(program)
        codes = {issue.code for issue in issues}
        self.assertIn("rule.unsafe_head_variable", codes)
        self.assertIn("rule.unsafe_negative_variable", codes)

    def test_negative_cycle_rejected_with_empty_data(self):
        _, _, program, issues = build("schema.json", "bad-negative-cycle.json")
        self.assertIsNone(program)
        negative_cycle_issues = [
            issue for issue in issues if issue.code == "stratification.negative_cycle"
        ]
        self.assertTrue(negative_cycle_issues)
        evidence = negative_cycle_issues[0].details["cycle"]
        self.assertTrue(evidence)
        self.assertTrue(any(edge["sign"] == "negative" for edge in evidence))
        for edge in evidence:
            self.assertIn("rule", edge)
        # The reported edges really do close a cycle through rule n1/n2.
        rule_ids = {edge["rule"] for edge in evidence}
        self.assertEqual(rule_ids, {"n1", "n2"})
        self.assertEqual(
            {(edge["from"], edge["to"]) for edge in evidence},
            {("eligible", "review"), ("review", "eligible")},
        )

    def test_extensional_head_and_fact_rejected(self):
        schema = datalog.Schema(
            arities={"a": 1, "b": 1}, extensional=("a",), intensional=("b",)
        )
        issues: list[datalog.Issue] = []
        rules = datalog.parse_rules(
            [
                {
                    "id": "x",
                    "head": {"predicate": "a", "args": ["?x"]},
                    "positive": [{"predicate": "a", "args": ["?x"]}],
                    "negative": [],
                }
            ],
            schema,
            issues,
        )
        self.assertTrue(any(i.code == "rule.extensional_head" for i in issues))
        facts, _, _ = parse_facts_text('{"predicate": "b", "args": ["z"]}\n', schema)
        self.assertEqual(facts, {})


class LanguageSemanticsTests(unittest.TestCase):
    def test_repeated_variables_and_constants(self):
        schema = datalog.Schema(
            arities={"e": 2, "loop": 1, "same": 2},
            extensional=("e",),
            intensional=("loop", "same"),
        )
        rules_raw = [
            {
                "id": "loop",
                "head": {"predicate": "loop", "args": ["?x"]},
                "positive": [{"predicate": "e", "args": ["?x", "?x"]}],
                "negative": [],
            },
            {
                "id": "same",
                "head": {"predicate": "same", "args": ["?x", "?y"]},
                "positive": [{"predicate": "e", "args": ["?x", "?y"]}],
                "negative": [{"predicate": "loop", "args": ["?x"]}],
            },
        ]
        issues: list[datalog.Issue] = []
        rules = datalog.parse_rules(rules_raw, schema, issues)
        program = datalog.build_program(schema, rules, issues)
        facts, counts, dup = parse_facts_text(
            '{"predicate": "e", "args": ["a", "a"]}\n'
            '{"predicate": "e", "args": ["b", "c"]}\n',
            schema,
        )
        evaluation = datalog.evaluate(program, facts, counts, dup)
        self.assertEqual(evaluation.relations["loop"], {("a",)})
        self.assertEqual(evaluation.relations["same"], {("b", "c")})
        self.assertTrue(datalog.verify_proofs(evaluation)["ok"])

    def test_multiple_recursive_atoms_in_one_rule(self):
        schema = datalog.Schema(
            arities={"edge": 2, "path": 2},
            extensional=("edge",),
            intensional=("path",),
        )
        rules_raw = [
            {
                "id": "base",
                "head": {"predicate": "path", "args": ["?x", "?y"]},
                "positive": [{"predicate": "edge", "args": ["?x", "?y"]}],
                "negative": [],
            },
            {
                "id": "compose",
                "head": {"predicate": "path", "args": ["?x", "?z"]},
                "positive": [
                    {"predicate": "path", "args": ["?x", "?y"]},
                    {"predicate": "path", "args": ["?y", "?z"]},
                ],
                "negative": [],
            },
        ]
        issues: list[datalog.Issue] = []
        rules = datalog.parse_rules(rules_raw, schema, issues)
        program = datalog.build_program(schema, rules, issues)
        facts, counts, dup = parse_facts_text(
            '{"predicate": "edge", "args": ["a", "b"]}\n'
            '{"predicate": "edge", "args": ["b", "c"]}\n'
            '{"predicate": "edge", "args": ["c", "d"]}\n'
            '{"predicate": "edge", "args": ["d", "b"]}\n',
            schema,
        )
        evaluation = datalog.evaluate(program, facts, counts, dup)
        self.assertEqual(
            evaluation.relations["path"],
            {
                ("a", "b"), ("a", "c"), ("a", "d"),
                ("b", "b"), ("b", "c"), ("b", "d"),
                ("c", "b"), ("c", "c"), ("c", "d"),
                ("d", "b"), ("d", "c"), ("d", "d"),
            },
        )
        oracle = datalog.naive_full_closure(program, facts)
        self.assertEqual(oracle["path"], evaluation.relations["path"])
        self.assertTrue(datalog.verify_proofs(evaluation)["ok"])

    def test_negation_waits_for_lower_stratum(self):
        # q needs two recursive rounds: q(b) is seeded in round 1 and q(c)
        # only appears in round 2.  out negates q from a higher stratum.  A
        # premature peek (after q's first round) would wrongly derive out(c).
        schema = datalog.Schema(
            arities={"edge": 2, "q": 1, "out": 1},
            extensional=("edge",),
            intensional=("q", "out"),
        )
        rules_raw = [
            {
                "id": "q1",
                "head": {"predicate": "q", "args": ["?y"]},
                "positive": [{"predicate": "edge", "args": ["a", "?y"]}],
                "negative": [],
            },
            {
                "id": "q2",
                "head": {"predicate": "q", "args": ["?z"]},
                "positive": [
                    {"predicate": "q", "args": ["?y"]},
                    {"predicate": "edge", "args": ["?y", "?z"]},
                ],
                "negative": [],
            },
            {
                "id": "out1",
                "head": {"predicate": "out", "args": ["?y"]},
                "positive": [{"predicate": "edge", "args": ["a", "?y"]}],
                "negative": [{"predicate": "q", "args": ["?y"]}],
            },
        ]
        issues: list[datalog.Issue] = []
        rules = datalog.parse_rules(rules_raw, schema, issues)
        program = datalog.build_program(schema, rules, issues)
        facts, counts, dup = parse_facts_text(
            '{"predicate": "edge", "args": ["a", "b"]}\n'
            '{"predicate": "edge", "args": ["b", "c"]}\n',
            schema,
        )
        evaluation = datalog.evaluate(program, facts, counts, dup)
        self.assertEqual(evaluation.relations["q"], {("b",), ("c",)})
        q_stratum = next(s for s in evaluation.strata_results if "q" in s.predicates)
        productive = [r for r in q_stratum.rounds if r.delta]
        self.assertGreaterEqual(len(productive), 2)
        # q is complete (b and c) before out is evaluated, so the negation
        # removes both candidates and out stays empty.
        self.assertEqual(evaluation.relations["out"], set())
        self.assertLess(
            program.predicate_stratum["q"], program.predicate_stratum["out"]
        )
        self.assertEqual(datalog.naive_full_closure(program, facts)["out"], set())
        self.assertTrue(datalog.verify_proofs(evaluation)["ok"])


class TinyBaselineTests(unittest.TestCase):
    def test_tiny_naive_matches_semi_naive_relation_by_relation(self):
        program, evaluation = run_fixture(rules_file="tiny-rules.json", facts_file="tiny-facts.jsonl")
        comparison = datalog.compare_with_naive(evaluation)
        self.assertTrue(comparison["ok"], comparison["differences"])
        for predicate in program.schema.arities:
            self.assertEqual(
                evaluation.relations[predicate],
                datalog.naive_full_closure(program, evaluation.input_relations)[predicate],
            )
        self.assertTrue(datalog.verify_proofs(evaluation)["ok"])
        self.assertEqual(
            evaluation.relations["reach"],
            {
                ("a", "a"), ("a", "b"), ("a", "c"),
                ("b", "a"), ("b", "b"), ("b", "c"),
            },
        )

    def test_tiny_proof_rounds_increase(self):
        _, evaluation = run_fixture(rules_file="tiny-rules.json", facts_file="tiny-facts.jsonl")
        for fact, proof in evaluation.proofs.items():
            if proof.source == "input":
                continue
            for _, premise in proof.positive:
                premise_proof = evaluation.proofs[premise]
                if premise_proof.source == "rule":
                    self.assertLess(premise_proof.round, proof.round)


if __name__ == "__main__":
    unittest.main()
