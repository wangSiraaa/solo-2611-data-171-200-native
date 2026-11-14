#!/usr/bin/env python3
"""Stratified Datalog evaluator for the small relational-rule fixtures.

Supported language (deliberately small):

* an atom is {"predicate": str, "args": [str, ...]}
* a term whose non-empty string starts with "?" is a variable; every other
  string is a constant compared literally
* a rule is one head atom plus a conjunction of positive atoms and a
  conjunction of "not" atoms; there is at least one positive atom
* no functions, arithmetic, comparisons, aggregates, NULL or anonymous
  variables; all positions are strings
* extensional predicates receive facts only from the JSONL input; all other
  declared predicates are intensional/derived and may be rule heads
* the program must be stratified: a dependency cycle may contain positive
  edges only.  Negative edges must point at a strictly lower stratum.

Evaluation is per stratum (strongly connected component of the predicate
dependency graph).  Positive recursion inside a stratum uses semi-naive
delta iteration.  Negation reads only completed lower strata.

Run ``python3 datalog.py --help`` or see ``RUNNING.md`` for the CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

MAX_PREDICATES = 8
MAX_RULES = 20

# A fact is (predicate, argument_tuple).  Relations map predicate -> set of
# argument tuples; the pair form is convenient for proofs.
Fact = tuple[str, tuple[str, ...]]
RelationMap = dict[str, set[tuple[str, ...]]]


# ---------------------------------------------------------------------------
# Problems found while loading or validating the program
# ---------------------------------------------------------------------------


def is_variable(term: str) -> bool:
    return isinstance(term, str) and term.startswith("?")


@dataclass(frozen=True)
class Atom:
    predicate: str
    args: tuple[str, ...]

    def variables(self) -> tuple[str, ...]:
        return tuple(a for a in self.args if is_variable(a))

    def to_json(self) -> dict[str, Any]:
        return {"predicate": self.predicate, "args": list(self.args)}


@dataclass(frozen=True)
class Rule:
    id: str
    head: Atom
    positive: tuple[Atom, ...]
    negative: tuple[Atom, ...]
    order: int


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    rule_id: str | None = None
    predicate: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.rule_id is not None:
            out["rule_id"] = self.rule_id
        if self.predicate is not None:
            out["predicate"] = self.predicate
        if self.details:
            out["details"] = self.details
        return out


@dataclass(frozen=True)
class Schema:
    arities: dict[str, int]
    extensional: tuple[str, ...]
    intensional: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "predicates": {p: self.arities[p] for p in sorted(self.arities)},
            "extensional": list(self.extensional),
            "intensional": list(self.intensional),
        }


@dataclass(frozen=True)
class Edge:
    source: str  # predicate in the body
    target: str  # predicate in the head
    sign: str  # "positive" | "negative"
    rule_id: str

    def to_json(self) -> dict[str, Any]:
        return {
            "from": self.source,
            "to": self.target,
            "sign": self.sign,
            "rule": self.rule_id,
        }


@dataclass
class Stratum:
    index: int
    predicates: tuple[str, ...]
    rules: tuple[str, ...]

    def to_json(self, program: "Program | None" = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "index": self.index,
            "predicates": list(self.predicates),
            "rules": list(self.rules),
        }
        if program is not None:
            out["recursive"] = any(
                rule_id in self.rules and program.is_recursive_rule(rule_id)
                for rule_id in self.rules
            )
        return out


@dataclass
class Program:
    schema: Schema
    rules: tuple[Rule, ...]
    strata: tuple[Stratum, ...]
    predicate_stratum: dict[str, int]
    edges: tuple[Edge, ...]
    limits: dict[str, Any]
    warnings: tuple[Issue, ...] = ()

    def rule(self, rule_id: str) -> Rule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise KeyError(rule_id)

    def is_recursive_rule(self, rule_id: str) -> bool:
        rule = self.rule(rule_id)
        stratum_preds = set(self.strata[self.predicate_stratum[rule.head.predicate]].predicates)
        return any(atom.predicate in stratum_preds for atom in rule.positive)

    def predicate_stratum_map(self) -> dict[str, int]:
        return dict(sorted(self.predicate_stratum.items()))

    def to_json(self) -> dict[str, Any]:
        return {
            "strata": [s.to_json(self) for s in self.strata],
            "predicate_stratum": self.predicate_stratum_map(),
            "dependency_edges": [e.to_json() for e in self.edges],
            "recursive_rules": [
                r.id for r in self.rules if self.is_recursive_rule(r.id)
            ],
        }


# ---------------------------------------------------------------------------
# Loading and structural validation
# ---------------------------------------------------------------------------


def load_json_file(path: Path, issues: list[Issue], what: str) -> Any | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        issues.append(Issue("io.read", f"cannot read {what} file {path}: {exc}"))
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        issues.append(
            Issue(
                "json.parse",
                f"invalid JSON in {what} file {path}: {exc.msg}",
                details={"line": exc.lineno, "column": exc.colno},
            )
        )
        return None


def parse_atom(
    raw: Any,
    issues: list[Issue],
    context: str,
    rule_id: str | None = None,
) -> Atom | None:
    if not isinstance(raw, dict):
        issues.append(
            Issue("atom.shape", f"{context}: atom must be a JSON object", rule_id=rule_id)
        )
        return None
    predicate = raw.get("predicate")
    args = raw.get("args")
    if not isinstance(predicate, str) or predicate == "":
        issues.append(
            Issue(
                "atom.predicate",
                f"{context}: atom needs a non-empty string 'predicate'",
                rule_id=rule_id,
            )
        )
        return None
    if not isinstance(args, list):
        issues.append(
            Issue(
                "atom.args",
                f"{context}: atom {predicate!r} needs an 'args' array",
                rule_id=rule_id,
                predicate=predicate,
            )
        )
        return None
    for pos, arg in enumerate(args):
        if not isinstance(arg, str):
            issues.append(
                Issue(
                    "atom.term",
                    f"{context}: argument {pos} of {predicate!r} must be a string",
                    rule_id=rule_id,
                    predicate=predicate,
                )
            )
            return None
    return Atom(predicate, tuple(args))


def parse_schema(raw: Any, issues: list[Issue]) -> Schema | None:
    if not isinstance(raw, dict):
        issues.append(Issue("schema.shape", "schema must be a JSON object"))
        return None
    preds_raw = raw.get("predicates")
    ext_raw = raw.get("extensional")
    if not isinstance(preds_raw, dict):
        issues.append(Issue("schema.predicates", "schema.predicates must be an object"))
        return None
    if not isinstance(ext_raw, list):
        issues.append(Issue("schema.extensional", "schema.extensional must be an array"))
        return None

    arities: dict[str, int] = {}
    for name, arity in preds_raw.items():
        if not isinstance(name, str) or name == "":
            issues.append(Issue("schema.predicate_name", "predicate names must be non-empty strings"))
            continue
        if not isinstance(arity, int) or isinstance(arity, bool) or arity < 0:
            issues.append(
                Issue(
                    "schema.arity",
                    f"arity of predicate {name!r} must be a non-negative integer",
                    predicate=name,
                )
            )
            continue
        arities[name] = arity

    extensional: list[str] = []
    for name in ext_raw:
        if not isinstance(name, str) or name == "":
            issues.append(Issue("schema.extensional", "extensional entries must be predicate names"))
            continue
        if name in extensional:
            issues.append(
                Issue(
                    "schema.extensional_duplicate",
                    f"extensional predicate {name!r} is listed more than once",
                    predicate=name,
                )
            )
            continue
        if name not in arities:
            issues.append(
                Issue(
                    "schema.undefined_predicate",
                    f"extensional predicate {name!r} is not declared in predicates",
                    predicate=name,
                )
            )
            continue
        extensional.append(name)

    if len(arities) > MAX_PREDICATES:
        issues.append(
            Issue(
                "schema.too_many_predicates",
                f"at most {MAX_PREDICATES} predicates are supported, got {len(arities)}",
                details={"limit": MAX_PREDICATES, "count": len(arities)},
            )
        )

    extensional_sorted = tuple(sorted(extensional))
    intensional = tuple(sorted(p for p in arities if p not in extensional))
    return Schema(arities=arities, extensional=extensional_sorted, intensional=intensional)


def parse_rules(
    raw: Any,
    schema: Schema,
    issues: list[Issue],
) -> tuple[Rule, ...]:
    if not isinstance(raw, list):
        issues.append(Issue("rules.shape", "rules must be a JSON array"))
        return ()
    if len(raw) > MAX_RULES:
        issues.append(
            Issue(
                "rules.too_many",
                f"at most {MAX_RULES} rules are supported, got {len(raw)}",
                details={"limit": MAX_RULES, "count": len(raw)},
            )
        )
    rules: list[Rule] = []
    seen_ids: set[str] = set()
    for position, raw_rule in enumerate(raw):
        if not isinstance(raw_rule, dict):
            issues.append(Issue("rule.shape", f"rule #{position} must be a JSON object"))
            continue
        rule_id = raw_rule.get("id")
        if not isinstance(rule_id, str) or rule_id == "":
            issues.append(Issue("rule.id", f"rule #{position} needs a non-empty string id"))
            continue
        if rule_id in seen_ids:
            issues.append(Issue("rule.id_duplicate", f"duplicate rule id {rule_id!r}", rule_id=rule_id))
            continue
        seen_ids.add(rule_id)

        head = parse_atom(raw_rule.get("head"), issues, f"rule {rule_id} head", rule_id)
        pos_raw = raw_rule.get("positive", [])
        neg_raw = raw_rule.get("negative", [])
        if not isinstance(pos_raw, list):
            issues.append(Issue("rule.positive", f"rule {rule_id}: positive must be an array", rule_id=rule_id))
            pos_raw = []
        if not isinstance(neg_raw, list):
            issues.append(Issue("rule.negative", f"rule {rule_id}: negative must be an array", rule_id=rule_id))
            neg_raw = []
        positive = tuple(
            atom
            for atom in (
                parse_atom(item, issues, f"rule {rule_id} positive atom", rule_id)
                for item in pos_raw
            )
            if atom is not None
        )
        negative = tuple(
            atom
            for atom in (
                parse_atom(item, issues, f"rule {rule_id} negative atom", rule_id)
                for item in neg_raw
            )
            if atom is not None
        )
        if not positive:
            issues.append(
                Issue(
                    "rule.no_positive",
                    f"rule {rule_id}: at least one positive body atom is required",
                    rule_id=rule_id,
                )
            )
        if head is None:
            continue

        # Predicate roles and arity.
        for kind, atom in [("head", head)] + [("positive", a) for a in positive] + [
            ("negative", a) for a in negative
        ]:
            if atom.predicate not in schema.arities:
                issues.append(
                    Issue(
                        "rule.undefined_predicate",
                        f"rule {rule_id}: predicate {atom.predicate!r} is not declared",
                        rule_id=rule_id,
                        predicate=atom.predicate,
                    )
                )
            elif len(atom.args) != schema.arities[atom.predicate]:
                issues.append(
                    Issue(
                        "rule.arity",
                        f"rule {rule_id}: predicate {atom.predicate!r} expects "
                        f"{schema.arities[atom.predicate]} args, got {len(atom.args)}",
                        rule_id=rule_id,
                        predicate=atom.predicate,
                        details={"expected": schema.arities[atom.predicate], "got": len(atom.args)},
                    )
                )
        if head.predicate in schema.extensional:
            issues.append(
                Issue(
                    "rule.extensional_head",
                    f"rule {rule_id}: extensional predicate {head.predicate!r} cannot be a rule head",
                    rule_id=rule_id,
                    predicate=head.predicate,
                )
            )

        # Safety: every variable in head and negative body must occur in a
        # positive body atom.  This also rejects negative-only variables.
        positive_variables = {v for atom in positive for v in atom.variables()}
        for atom in positive:
            for arg in atom.args:
                if is_variable(arg):
                    positive_variables.add(arg)
        head_variables = set(head.variables())
        missing_head = sorted(head_variables - positive_variables)
        if missing_head:
            issues.append(
                Issue(
                    "rule.unsafe_head_variable",
                    f"rule {rule_id}: head variables {missing_head} are not bound by any positive body atom",
                    rule_id=rule_id,
                    details={"variables": missing_head},
                )
            )
        for atom in negative:
            missing_neg = sorted(set(atom.variables()) - positive_variables)
            if missing_neg:
                issues.append(
                    Issue(
                        "rule.unsafe_negative_variable",
                        f"rule {rule_id}: negative atom {atom.predicate!r} uses variables "
                        f"{missing_neg} that are not bound by any positive body atom",
                        rule_id=rule_id,
                        predicate=atom.predicate,
                        details={"variables": missing_neg},
                    )
                )
        rules.append(
            Rule(
                id=rule_id,
                head=head,
                positive=positive,
                negative=negative,
                order=position,
            )
        )
    return tuple(rules)


def parse_facts(
    path: Path,
    schema: Schema,
    issues: list[Issue],
) -> tuple[RelationMap, dict[Fact, int], int]:
    """Return (deduplicated facts, occurrence counts, duplicate count)."""
    raw: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        issues.append(Issue("io.read", f"cannot read facts file {path}: {exc}"))
        return {}, {}, 0
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append(
                Issue(
                    "facts.json",
                    f"facts line {line_no}: invalid JSON: {exc.msg}",
                    details={"line": line_no, "column": exc.colno},
                )
            )
            continue
        atom = parse_atom(obj, issues, f"facts line {line_no}")
        if atom is None:
            continue
        if atom.predicate not in schema.arities:
            issues.append(
                Issue(
                    "fact.undefined_predicate",
                    f"facts line {line_no}: predicate {atom.predicate!r} is not declared",
                    predicate=atom.predicate,
                    details={"line": line_no},
                )
            )
            continue
        expected = schema.arities[atom.predicate]
        if len(atom.args) != expected:
            issues.append(
                Issue(
                    "fact.arity",
                    f"facts line {line_no}: predicate {atom.predicate!r} expects "
                    f"{expected} args, got {len(atom.args)}",
                    predicate=atom.predicate,
                    details={"line": line_no, "expected": expected, "got": len(atom.args)},
                )
            )
            continue
        if atom.predicate not in schema.extensional:
            issues.append(
                Issue(
                    "fact.intensional_predicate",
                    f"facts line {line_no}: predicate {atom.predicate!r} is intensional; "
                    "only extensional predicates may receive input facts",
                    predicate=atom.predicate,
                    details={"line": line_no},
                )
            )
            continue
        raw.append({"predicate": atom.predicate, "args": list(atom.args)})

    counts: dict[Fact, int] = {}
    facts: RelationMap = {}
    for obj in raw:
        atom = Atom(obj["predicate"], tuple(obj["args"]))
        fact: Fact = (atom.predicate, atom.args)
        counts[fact] = counts.get(fact, 0) + 1
        facts.setdefault(atom.predicate, set()).add(atom.args)
    duplicates = sum(count - 1 for count in counts.values())
    return facts, counts, duplicates


def parse_queries(raw: Any, schema: Schema, issues: list[Issue]) -> tuple[Atom, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        issues.append(Issue("queries.shape", "queries must be a JSON array"))
        return ()
    queries: list[Atom] = []
    seen: set[Fact] = set()
    for position, item in enumerate(raw):
        atom = parse_atom(item, issues, f"query #{position}")
        if atom is None:
            continue
        if atom.predicate not in schema.arities:
            issues.append(
                Issue(
                    "query.undefined_predicate",
                    f"query #{position}: predicate {atom.predicate!r} is not declared",
                    predicate=atom.predicate,
                )
            )
            continue
        expected = schema.arities[atom.predicate]
        if len(atom.args) != expected:
            issues.append(
                Issue(
                    "query.arity",
                    f"query #{position}: predicate {atom.predicate!r} expects "
                    f"{expected} args, got {len(atom.args)}",
                    predicate=atom.predicate,
                    details={"expected": expected, "got": len(atom.args)},
                )
            )
            continue
        fact: Fact = (atom.predicate, atom.args)
        if fact in seen:
            issues.append(
                Issue(
                    "query.duplicate",
                    f"query #{position}: duplicate query {atom.to_json()}",
                    predicate=atom.predicate,
                )
            )
        seen.add(fact)
        queries.append(atom)
    for position, atom in enumerate(queries):
        for arg in atom.args:
            if is_variable(arg):
                issues.append(
                    Issue(
                        "query.variable",
                        f"query #{position}: queries must be ground; got variable {arg!r}",
                        predicate=atom.predicate,
                    )
                )
    return tuple(queries)


# ---------------------------------------------------------------------------
# Dependency graph and stratification
# ---------------------------------------------------------------------------


@dataclass
class Dependency:
    graph: dict[str, set[str]]
    edges: list[Edge]
    sccs: list[tuple[str, ...]]
    scc_of: dict[str, int]
    order: list[int]  # SCC ids in evaluation order


def build_dependencies(rules: Sequence[Rule], schema: Schema) -> Dependency:
    graph: dict[str, set[str]] = {p: set() for p in schema.arities}
    edges: list[Edge] = []
    for rule in rules:
        # Undefined predicates are already reported by parse_rules; keep the
        # graph total here so diagnostics can still be produced together.
        graph.setdefault(rule.head.predicate, set())
        for atom in rule.positive:
            graph.setdefault(atom.predicate, set()).add(rule.head.predicate)
            edges.append(Edge(atom.predicate, rule.head.predicate, "positive", rule.id))
        for atom in rule.negative:
            graph.setdefault(atom.predicate, set()).add(rule.head.predicate)
            edges.append(Edge(atom.predicate, rule.head.predicate, "negative", rule.id))
    return Dependency(graph=graph, edges=edges, sccs=[], scc_of={}, order=[])


def tarjan_scc(graph: Mapping[str, set[str]]) -> list[tuple[str, ...]]:
    """Deterministic Tarjan SCC over the (small) predicate graph."""
    nodes = sorted(graph)
    adjacency = {node: sorted(graph[node]) for node in nodes}
    index_counter = 0
    stack: list[str] = []
    on_stack: dict[str, bool] = {node: False for node in nodes}
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    result: list[tuple[str, ...]] = []

    def strongconnect(v: str) -> None:
        nonlocal index_counter
        indices[v] = index_counter
        lowlink[v] = index_counter
        index_counter += 1
        stack.append(v)
        on_stack[v] = True
        for w in adjacency[v]:
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif on_stack[w]:
                lowlink[v] = min(lowlink[v], indices[w])
        if lowlink[v] == indices[v]:
            component: list[str] = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                component.append(w)
                if w == v:
                    break
            result.append(tuple(sorted(component)))

    for node in nodes:
        if node not in indices:
            strongconnect(node)
    return result


def find_cycle_with_negative_edge(
    scc: set[str],
    graph: Mapping[str, set[str]],
    negative_edges: Sequence[Edge],
    edges_by_pair: Mapping[tuple[str, str], list[Edge]],
) -> list[Edge]:
    """Return one concrete cycle through a negative edge inside an SCC.

    The cycle is returned as a list of provenance edges: a path of positive
    and negative body->head dependency edges, containing at least one
    negative edge.  It is the evidence a human needs to fix stratification.
    """
    for neg in negative_edges:
        u, v = neg.source, neg.target
        # Find path v -> ... -> u inside the SCC (any edge sign), so that
        # appending u --neg--> v closes a cycle.
        parent: dict[str, str | None] = {v: None}
        queue: list[str] = [v]
        found = v == u
        while queue and not found:
            cur = queue.pop(0)
            for nxt in sorted(graph[cur]):
                if nxt not in scc or nxt in parent:
                    continue
                parent[nxt] = cur
                if nxt == u:
                    found = True
                    break
                queue.append(nxt)
        if not found:
            continue
        path_nodes = [u]
        cur: str | None = u
        while cur is not None and cur != v:
            cur = parent[cur]
            if cur is not None:
                path_nodes.append(cur)
        path_nodes.reverse()  # v ... u
        cycle: list[Edge] = []
        for source, target in zip(path_nodes, path_nodes[1:]):
            for edge in edges_by_pair.get((source, target), []):
                cycle.append(edge)
                break
        cycle.append(neg)
        return cycle
    return []


def stratify(dep: Dependency, rules: Sequence[Rule], schema: Schema) -> tuple[list[Stratum] | None, list[Issue]]:
    issues: list[Issue] = []
    sccs = tarjan_scc(dep.graph)
    scc_of: dict[str, int] = {}
    for index, component in enumerate(sccs):
        for pred in component:
            scc_of[pred] = index
    dep.sccs = sccs
    dep.scc_of = scc_of

    # Any negative dependency inside one SCC is a negative cycle.  Refuse it
    # statically, independently of whether the current data is empty.
    edges_by_pair: dict[tuple[str, str], list[Edge]] = {}
    for edge in dep.edges:
        edges_by_pair.setdefault((edge.source, edge.target), []).append(edge)
    negative_inside: list[Edge] = []
    for edge in dep.edges:
        if edge.sign == "negative" and scc_of[edge.source] == scc_of[edge.target]:
            negative_inside.append(edge)
    if negative_inside:
        for edge in negative_inside:
            component = set(sccs[scc_of[edge.source]])
            cycle = find_cycle_with_negative_edge(component, dep.graph, [edge], edges_by_pair)
            issues.append(
                Issue(
                    "stratification.negative_cycle",
                    "negative dependency cycle: "
                    f"{edge.source!r} -> {edge.target!r} via rule {edge.rule_id}; "
                    "a cycle containing a negative edge cannot be stratified",
                    rule_id=edge.rule_id,
                    predicate=edge.source,
                    details={
                        "component": sorted(component),
                        "negative_rule": edge.rule_id,
                        "cycle": [e.to_json() for e in cycle],
                    },
                )
            )
        return None, issues

    # Condensation DAG over SCCs.  Every edge A -> B means A must be evaluated
    # before B (positive body before head; negative body before head too, and
    # the rejected negative cycle case cannot put them in one SCC).
    scc_count = len(sccs)
    condensation: dict[int, set[int]] = {i: set() for i in range(scc_count)}
    indegree = {i: 0 for i in range(scc_count)}
    for edge in dep.edges:
        a, b = scc_of[edge.source], scc_of[edge.target]
        if a != b and b not in condensation[a]:
            condensation[a].add(b)
            indegree[b] += 1

    import heapq

    heap = [(min(sccs[i]), i) for i in range(scc_count) if indegree[i] == 0]
    heapq.heapify(heap)
    order: list[int] = []
    while heap:
        _, node = heapq.heappop(heap)
        order.append(node)
        for nxt in sorted(condensation[node], key=lambda i: min(sccs[i])):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                heapq.heappush(heap, (min(sccs[nxt]), nxt))
    if len(order) != scc_count:
        issues.append(Issue("stratification.cycle", "predicate dependency graph is not a DAG"))
        return None, issues
    dep.order = order

    # The extensional predicates form the completed fact base.  They cannot
    # be rule heads and therefore have no incoming dependencies, so their
    # SCCs are isolated nodes in the condensation; group them into one base
    # stratum so that every derived predicate sits above a completed layer.
    ext = set(schema.extensional)
    base_predicates = tuple(sorted(p for p in schema.extensional))
    derived_scc_ids = [
        scc_id
        for scc_id in order
        if not all(pred in ext for pred in sccs[scc_id])
    ]
    strata: list[Stratum] = []
    if base_predicates:
        strata.append(
            Stratum(index=0, predicates=base_predicates, rules=())
        )
    for scc_id in derived_scc_ids:
        predicates = tuple(sorted(sccs[scc_id]))
        # Every predicate of this SCC shares a layer.  Rules whose head is in
        # the SCC belong here; rules for undefined heads were already
        # reported by parse_rules and never reach this path.
        rules_here = tuple(r.id for r in rules if r.head.predicate in predicates)
        strata.append(
            Stratum(
                index=len(strata),
                predicates=predicates,
                rules=rules_here,
            )
        )
    return strata, issues


def build_program(
    schema: Schema,
    rules: tuple[Rule, ...],
    issues: list[Issue],
) -> Program | None:
    dep = build_dependencies(rules, schema)
    strata, strat_issues = stratify(dep, rules, schema)
    issues.extend(strat_issues)
    if strata is None:
        return None
    if issues:
        # A structurally invalid rule (unsafe variables, bad arity, wrong
        # predicate role, ...) rejects the whole program.  We still ran
        # stratification above so dependent errors are reported together.
        return None
    predicate_stratum: dict[str, int] = {}
    for stratum in strata:
        for predicate in stratum.predicates:
            predicate_stratum[predicate] = stratum.index
    warnings: list[Issue] = []
    for pred in schema.intensional:
        if not any(rule.head.predicate == pred for rule in rules):
            warnings.append(
                Issue(
                    "predicate.no_rules",
                    f"intensional predicate {pred!r} has no defining rule and stays empty",
                    predicate=pred,
                )
            )
    return Program(
        schema=schema,
        rules=rules,
        strata=tuple(strata),
        predicate_stratum=predicate_stratum,
        edges=tuple(dep.edges),
        limits={"predicates": MAX_PREDICATES, "rules": MAX_RULES},
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NegativeCheck:
    atom: Atom
    predicate: str
    stratum: int
    stratum_complete: bool
    missing: bool
    relation_size: int

    def to_json(self) -> dict[str, Any]:
        return {
            "atom": self.atom.to_json(),
            "predicate": self.predicate,
            "checked_stratum": self.stratum,
            "stratum_complete": self.stratum_complete,
            "missing": self.missing,
            "relation_size": self.relation_size,
        }


@dataclass(frozen=True)
class Proof:
    fact: Fact
    source: str  # "input" or "rule"
    rule_id: str | None
    bindings: dict[str, str]
    positive: tuple[tuple[int, Fact], ...]  # (body atom index, premise fact)
    negative: tuple[NegativeCheck, ...]
    stratum: int
    round: int  # -1 for input facts, otherwise 1-based productive round

    def to_json(self, proof_sources: Mapping[Fact, str] | None = None) -> dict[str, Any]:
        sources = proof_sources or {}
        return {
            "fact": {"predicate": self.fact[0], "args": list(self.fact[1])},
            "source": self.source,
            "rule_id": self.rule_id,
            "bindings": dict(sorted(self.bindings.items())),
            "positive": [
                {
                    "atom_index": index,
                    "predicate": fact[0],
                    "args": list(fact[1]),
                    "source": sources.get(fact, "derived"),
                }
                for index, fact in self.positive
            ],
            "negative": [n.to_json() for n in self.negative],
            "stratum": self.stratum,
            "round": self.round,
        }


@dataclass
class RuleStat:
    variants: int = 0
    delta_variants: list[dict[str, Any]] = field(default_factory=list)
    join_probes: int = 0
    complete_matches: int = 0
    duplicate_matches: int = 0
    negative_rejected: int = 0
    new_facts: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "variants": self.variants,
            "delta_variants": list(self.delta_variants),
            "join_probes": self.join_probes,
            "complete_matches": self.complete_matches,
            "duplicate_matches": self.duplicate_matches,
            "negative_rejected": self.negative_rejected,
            "new_facts": self.new_facts,
        }


@dataclass
class RoundReport:
    round: int
    delta: dict[str, int]
    total_after: dict[str, int]
    rules: dict[str, RuleStat]

    def to_json(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "delta": dict(sorted(self.delta.items())),
            "delta_size": sum(self.delta.values()),
            "total_after": dict(sorted(self.total_after.items())),
            "joins": {rule_id: stat.to_json() for rule_id, stat in sorted(self.rules.items())},
        }


@dataclass
class Candidate:
    fact: Fact
    rule: Rule
    bindings: dict[str, str]
    positive: tuple[tuple[int, Fact], ...]
    negative: tuple[NegativeCheck, ...]
    stratum: int
    round: int


@dataclass
class StratumResult:
    index: int
    predicates: tuple[str, ...]
    rules: tuple[str, ...]
    rounds: list[RoundReport]
    relations: dict[str, list[tuple[str, ...]]]
    stable_at_round: int
    recursive: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "predicates": list(self.predicates),
            "rules": list(self.rules),
            "recursive": self.recursive,
            "relations": {
                pred: [list(args) for args in sorted(rows)]
                for pred, rows in sorted(self.relations.items())
            },
            "fact_counts": {pred: len(rows) for pred, rows in sorted(self.relations.items())},
            "rounds": [r.to_json() for r in self.rounds],
            "stable_at_round": self.stable_at_round,
        }


@dataclass
class Evaluation:
    program: Program
    input_relations: RelationMap
    input_counts: dict[Fact, int]
    duplicate_input_facts: int
    relations: RelationMap
    proofs: dict[Fact, Proof]
    strata_results: list[StratumResult]

    def to_json(self, include_proofs: bool = True) -> dict[str, Any]:
        total_productive_rounds = sum(
            1 for s in self.strata_results for r in s.rounds if r.delta
        )
        total_rounds = sum(len(s.rounds) for s in self.strata_results)
        total_probes = sum(
            stat.join_probes for s in self.strata_results for r in s.rounds for stat in r.rules.values()
        )
        total_matches = sum(
            stat.complete_matches
            for s in self.strata_results
            for r in s.rounds
            for stat in r.rules.values()
        )
        total_duplicates = sum(
            stat.duplicate_matches
            for s in self.strata_results
            for r in s.rounds
            for stat in r.rules.values()
        )
        total_negative_rejected = sum(
            stat.negative_rejected
            for s in self.strata_results
            for r in s.rounds
            for stat in r.rules.values()
        )
        out: dict[str, Any] = {
            "status": "ok",
            "schema": self.program.schema.to_json(),
            "program": self.program.to_json(),
            "input": {
                "fact_counts": {
                    pred: len(rows) for pred, rows in sorted(self.input_relations.items())
                },
                "total": sum(len(rows) for rows in self.input_relations.values()),
                "duplicates_removed": self.duplicate_input_facts,
            },
            "relations": {
                pred: [list(args) for args in sorted(rows)]
                for pred, rows in sorted(self.relations.items())
            },
            "fact_counts": {
                pred: len(rows) for pred, rows in sorted(self.relations.items())
            },
            "strata": [s.to_json() for s in self.strata_results],
            "statistics": {
                "reported_rounds": total_rounds,
                "productive_rounds": total_productive_rounds,
                "join_probes": total_probes,
                "complete_matches": total_matches,
                "duplicate_matches": total_duplicates,
                "negative_rejected": total_negative_rejected,
                "derived_facts": sum(
                    1
                    for fact, proof in self.proofs.items()
                    if proof.source == "rule"
                ),
            },
            "warnings": [w.to_json() for w in self.program.warnings],
        }
        if include_proofs:
            out["proofs"] = {
                fact_key(fact): proof.to_json(
                    {
                        premise: self.proofs[premise].source
                        for _, premise in proof.positive
                        if premise in self.proofs
                    }
                )
                for fact, proof in sorted(self.proofs.items())
                if proof.source == "rule"
            }
        return out


def fact_key(fact: Fact) -> str:
    predicate, args = fact
    rendered = ",".join(repr(arg) if not _is_simple(arg) else arg for arg in args)
    return f"{predicate}({rendered})"


def _is_simple(arg: str) -> bool:
    return arg.isalnum() or (arg and all(c.isalnum() or c in "_-." for c in arg))


def candidate_key(candidate: "Candidate") -> tuple[Any, ...]:
    """Deterministic proof preference, independent of input ordering.

    Among several rule instances that derive the same fact in one round we
    keep the lexicographically smallest (rule id, bindings, premises).  The
    task allows a freely chosen single proof; this makes the choice stable
    when the facts file or rule file is reordered.
    """
    return (
        candidate.rule.id,
        tuple(sorted(candidate.bindings.items())),
        tuple(candidate.positive),
    )


def consider_candidate(
    candidates: dict[Fact, "Candidate"], candidate: "Candidate"
) -> bool:
    """Insert candidate, keeping the deterministic best proof.  True if new."""
    existing = candidates.get(candidate.fact)
    if existing is None:
        candidates[candidate.fact] = candidate
        return True
    if candidate_key(candidate) < candidate_key(existing):
        candidates[candidate.fact] = candidate
    return False


def instantiate(atom: Atom, bindings: Mapping[str, str]) -> Fact | None:
    args: list[str] = []
    for term in atom.args:
        if is_variable(term):
            if term not in bindings:
                return None
            args.append(bindings[term])
        else:
            args.append(term)
    return atom.predicate, tuple(args)


def match_tuple(
    atom: Atom,
    row: tuple[str, ...],
    bindings: Mapping[str, str],
) -> dict[str, str] | None:
    """Extend ``bindings`` so that ``atom`` matches ``row``, or return None.

    Repeated variables and constants are enforced here; this is the only
    place where joins are decided.
    """
    extended = dict(bindings)
    for term, value in zip(atom.args, row):
        if is_variable(term):
            previous = extended.get(term)
            if previous is None:
                extended[term] = value
            elif previous != value:
                return None
        elif term != value:
            return None
    return extended


def ordered_positive_atoms(
    rule: Rule,
    delta_index: int | None,
) -> list[int]:
    indices = list(range(len(rule.positive)))
    if delta_index is not None and delta_index in indices:
        indices.remove(delta_index)
        indices.insert(0, delta_index)
    return indices


def evaluate_rule_variant(
    rule: Rule,
    delta_index: int | None,
    relations: Mapping[str, set[tuple[str, ...]]],
    delta: Mapping[str, set[tuple[str, ...]]],
) -> tuple[list[tuple[dict[str, str], tuple[tuple[int, Fact], ...]]], int]:
    """Nested-loop join for one rule instance variant.

    When ``delta_index`` is not None, that positive body atom is read from
    ``delta`` and all other atoms from the full current relations.  Returns
    the complete matches (bindings + ``(body index, fact)`` premises) and the
    number of (binding, tuple) probes actually performed.
    """
    order = ordered_positive_atoms(rule, delta_index)
    states: list[tuple[dict[str, str], tuple[tuple[int, Fact], ...]]] = [({}, ())]
    probes = 0
    for atom_index in order:
        atom = rule.positive[atom_index]
        if delta_index is not None and atom_index == delta_index:
            rows = delta.get(atom.predicate, set())
        else:
            rows = relations.get(atom.predicate, set())
        next_states: list[tuple[dict[str, str], tuple[tuple[int, Fact], ...]]] = []
        for bindings, premises in states:
            for row in sorted(rows):
                probes += 1
                extended = match_tuple(atom, row, bindings)
                if extended is None:
                    continue
                next_states.append(
                    (extended, premises + ((atom_index, (atom.predicate, row)),))
                )
        states = next_states
        if not states:
            break
    # Sort premises by body position to make proofs independent of join order.
    return [(b, tuple(sorted(p))) for b, p in states], probes


def check_negative(
    rule: Rule,
    bindings: Mapping[str, str],
    relations: Mapping[str, set[tuple[str, ...]]],
    predicate_stratum: Mapping[str, int],
    head_stratum: int,
) -> tuple[bool, list[NegativeCheck]]:
    """Return (all_missing, checks) for one ground rule instance."""
    checks: list[NegativeCheck] = []
    for atom in rule.negative:
        fact = instantiate(atom, bindings)
        if fact is None:  # defensive; safety validation rejects this
            raise ValueError(
                f"rule {rule.id}: unbound variable in negative atom {atom.to_json()}"
            )
        predicate = fact[0]
        stratum = predicate_stratum[predicate]
        if stratum >= head_stratum:
            raise ValueError(
                f"rule {rule.id}: negative atom {predicate!r} is not in a "
                "strictly lower completed stratum"
            )
        rows = relations.get(predicate, set())
        missing = fact[1] not in rows
        checks.append(
            NegativeCheck(
                atom=Atom(predicate, fact[1]),
                predicate=predicate,
                stratum=stratum,
                stratum_complete=True,
                missing=missing,
                relation_size=len(rows),
            )
        )
    return all(check.missing for check in checks), checks


def evaluate(
    program: Program,
    input_relations: RelationMap,
    input_counts: dict[Fact, int],
    duplicate_input_facts: int,
) -> Evaluation:
    """Evaluate the program stratum by stratum.

    Relations are global and are extended as strata complete.  A stratum's
    negation only reads predicates whose stratum index is strictly smaller,
    and those strata have completed before the rule can fire.
    """
    relations: RelationMap = {pred: set() for pred in program.schema.arities}
    for predicate, rows in input_relations.items():
        relations[predicate] |= set(rows)

    proofs: dict[Fact, Proof] = {}
    for predicate, rows in input_relations.items():
        stratum = program.predicate_stratum[predicate]
        for row in rows:
            fact: Fact = (predicate, row)
            proofs[fact] = Proof(
                fact=fact,
                source="input",
                rule_id=None,
                bindings={},
                positive=(),
                negative=(),
                stratum=stratum,
                round=-1,
            )

    strata_results: list[StratumResult] = []
    for stratum in program.strata:
        stratum_predicates = set(stratum.predicates)
        stratum_rules = [program.rule(rule_id) for rule_id in stratum.rules]
        base_rules = [
            rule
            for rule in stratum_rules
            if not any(atom.predicate in stratum_predicates for atom in rule.positive)
        ]
        recursive_rules = [
            rule
            for rule in stratum_rules
            if any(atom.predicate in stratum_predicates for atom in rule.positive)
        ]

        rounds: list[RoundReport] = []
        delta: dict[str, set[tuple[str, ...]]] = {p: set() for p in stratum.predicates}
        round_no = 0

        if not stratum_rules:
            # Extensionally populated stratum or an empty intensional one.
            stratum_relations = {
                pred: sorted(relations[pred]) for pred in stratum.predicates
            }
            strata_results.append(
                StratumResult(
                    index=stratum.index,
                    predicates=stratum.predicates,
                    rules=stratum.rules,
                    rounds=[],
                    relations=stratum_relations,
                    stable_at_round=0,
                    recursive=False,
                )
            )
            continue

        # Round 1: non-recursive (base) rules.  Recursive rules cannot fire
        # yet because the stratum delta is empty.
        round_no = 1
        candidates: dict[Fact, Candidate] = {}
        rule_stats: dict[str, RuleStat] = {}
        for rule in base_rules:
            stat = RuleStat(variants=1)
            matches, probes = evaluate_rule_variant(rule, None, relations, delta)
            stat.join_probes += probes
            stat.complete_matches += len(matches)
            for bindings, premises in matches:
                head_fact = instantiate(rule.head, bindings)
                if head_fact is None:  # safety validation should avoid this
                    continue
                if head_fact[1] in relations[head_fact[0]]:
                    stat.duplicate_matches += 1
                    continue
                ok, checks = check_negative(
                    rule, bindings, relations, program.predicate_stratum, stratum.index
                )
                if not ok:
                    stat.negative_rejected += 1
                    continue
                added = consider_candidate(
                    candidates,
                    Candidate(
                        fact=head_fact,
                        rule=rule,
                        bindings=bindings,
                        positive=premises,
                        negative=tuple(checks),
                        stratum=stratum.index,
                        round=round_no,
                    ),
                )
                if added:
                    stat.new_facts += 1
                else:
                    stat.duplicate_matches += 1
            rule_stats[rule.id] = stat

        new_facts_by_pred: dict[str, set[tuple[str, ...]]] = {
            p: set() for p in stratum.predicates
        }
        for fact in sorted(candidates):
            predicate, row = fact
            relations[predicate].add(row)
            new_facts_by_pred[predicate].add(row)
            candidate = candidates[fact]
            proofs[fact] = Proof(
                fact=fact,
                source="rule",
                rule_id=candidate.rule.id,
                bindings=dict(candidate.bindings),
                positive=candidate.positive,
                negative=candidate.negative,
                stratum=stratum.index,
                round=round_no,
            )
        delta = new_facts_by_pred
        # Always record the base round, even when it derives nothing: it
        # documents that the non-recursive contribution was exhausted.
        rounds.append(
            RoundReport(
                round=round_no,
                delta={
                    p: len(rows) for p, rows in new_facts_by_pred.items() if rows
                },
                total_after={p: len(relations[p]) for p in stratum.predicates},
                rules=rule_stats,
            )
        )

        # Rounds 2+: semi-naive deltas.  Only recursive rules participate.
        while any(delta.values()) and recursive_rules:
            round_no += 1
            candidates = {}
            rule_stats = {}
            for rule in recursive_rules:
                stat = RuleStat()
                delta_indices = [
                    i
                    for i, atom in enumerate(rule.positive)
                    if atom.predicate in stratum_predicates
                ]
                for atom_index in delta_indices:
                    atom = rule.positive[atom_index]
                    if not delta.get(atom.predicate):
                        # A variant with an empty delta cannot contribute a
                        # new fact under semi-naive evaluation.
                        continue
                    stat.variants += 1
                    stat.delta_variants.append(
                        {
                            "atom_index": atom_index,
                            "predicate": atom.predicate,
                            "delta_size": len(delta[atom.predicate]),
                        }
                    )
                    matches, probes = evaluate_rule_variant(
                        rule, atom_index, relations, delta
                    )
                    stat.join_probes += probes
                    stat.complete_matches += len(matches)
                    for bindings, premises in matches:
                        head_fact = instantiate(rule.head, bindings)
                        if head_fact is None:
                            continue
                        if head_fact[1] in relations[head_fact[0]]:
                            stat.duplicate_matches += 1
                            continue
                        ok, checks = check_negative(
                            rule,
                            bindings,
                            relations,
                            program.predicate_stratum,
                            stratum.index,
                        )
                        if not ok:
                            stat.negative_rejected += 1
                            continue
                        added = consider_candidate(
                            candidates,
                            Candidate(
                                fact=head_fact,
                                rule=rule,
                                bindings=bindings,
                                positive=premises,
                                negative=tuple(checks),
                                stratum=stratum.index,
                                round=round_no,
                            ),
                        )
                        if added:
                            stat.new_facts += 1
                        else:
                            stat.duplicate_matches += 1
                if stat.variants:
                    rule_stats[rule.id] = stat

            new_facts_by_pred = {p: set() for p in stratum.predicates}
            for fact in sorted(candidates):
                predicate, row = fact
                relations[predicate].add(row)
                new_facts_by_pred[predicate].add(row)
                candidate = candidates[fact]
                proofs[fact] = Proof(
                    fact=fact,
                    source="rule",
                    rule_id=candidate.rule.id,
                    bindings=dict(candidate.bindings),
                    positive=candidate.positive,
                    negative=candidate.negative,
                    stratum=stratum.index,
                    round=round_no,
                )
            if not any(new_facts_by_pred.values()):
                # Stable: no earlier-round delta combination produced a new
                # fact.  The empty round is recorded as the stopping point.
                rounds.append(
                    RoundReport(
                        round=round_no,
                        delta={},
                        total_after={
                            p: len(relations[p]) for p in stratum.predicates
                        },
                        rules=rule_stats,
                    )
                )
                delta = new_facts_by_pred
                break
            delta = new_facts_by_pred
            rounds.append(
                RoundReport(
                    round=round_no,
                    delta={
                        p: len(rows)
                        for p, rows in new_facts_by_pred.items()
                        if rows
                    },
                    total_after={p: len(relations[p]) for p in stratum.predicates},
                    rules=rule_stats,
                )
            )
        if rounds and any(rounds[-1].delta.values()):
            # A non-recursive stratum reaches its fixed point after the base
            # round; record the exhausted next round as the stopping point,
            # mirroring the recursive case.
            round_no += 1
            rounds.append(
                RoundReport(
                    round=round_no,
                    delta={},
                    total_after={p: len(relations[p]) for p in stratum.predicates},
                    rules={},
                )
            )

        strata_results.append(
            StratumResult(
                index=stratum.index,
                predicates=stratum.predicates,
                rules=stratum.rules,
                rounds=rounds,
                relations={pred: sorted(relations[pred]) for pred in stratum.predicates},
                stable_at_round=round_no,
                recursive=bool(recursive_rules),
            )
        )

    return Evaluation(
        program=program,
        input_relations=input_relations,
        input_counts=input_counts,
        duplicate_input_facts=duplicate_input_facts,
        relations=relations,
        proofs=proofs,
        strata_results=strata_results,
    )


# ---------------------------------------------------------------------------
# Explanation: render a proof and verify it by replay
# ---------------------------------------------------------------------------


def render_atom(fact: Fact) -> str:
    return f"{fact[0]}({', '.join(fact[1])})" if fact[1] else f"{fact[0]}()"


def render_proof(
    evaluation: Evaluation,
    fact: Fact,
    depth: int = 0,
    seen: set[Fact] | None = None,
    lines: list[str] | None = None,
) -> list[str]:
    """Render a finite proof tree.

    Proofs are acyclic by construction: a derived fact is only recorded after
    all of its positive premises already exist in a completed earlier round
    (or are input facts).  The ``seen`` guard is defensive only.
    """
    seen = set() if seen is None else seen
    lines = [] if lines is None else lines
    indent = "  " * depth
    proof = evaluation.proofs.get(fact)
    label = render_atom(fact)
    if proof is None:
        lines.append(f"{indent}{label}  [NOT DERIVED]")
        return lines
    if proof.source == "input":
        lines.append(f"{indent}{label}  [input]")
        return lines
    lines.append(
        f"{indent}{label}  [derived by {proof.rule_id}, stratum {proof.stratum}, round {proof.round}]"
    )
    if fact in seen:
        lines.append(f"{indent}  (proof recursion guard hit)")
        return lines
    seen.add(fact)
    binding_text = ", ".join(f"{k}={v}" for k, v in sorted(proof.bindings.items()))
    lines.append(f"{indent}  bindings: {binding_text}")
    for atom_index, premise in proof.positive:
        lines.append(f"{indent}  + {render_atom(premise)}")
        render_proof(evaluation, premise, depth + 2, seen, lines)
    for check in proof.negative:
        status = "absent" if check.missing else "PRESENT (invalid proof)"
        lines.append(
            f"{indent}  not {render_atom((check.predicate, check.atom.args))}  "
            f"[stratum {check.stratum} complete, {status}]"
        )
    seen.discard(fact)
    return lines


def verify_proofs(evaluation: Evaluation) -> dict[str, Any]:
    """Replay every stored proof against its rule and the final relations.

    Checks performed:
    * the rule exists and the head instantiates to the proved fact;
    * every positive premise is an actual body atom instance and exists
      (and was already present when the fact was derived);
    * every negative atom is ground, absent from its relation, and its
      predicate belongs to a strictly lower, therefore completed, stratum;
    * premises are acyclic (derived premise appears in an earlier round or
      an input fact), so no cyclic proof exists.
    """
    problems: list[dict[str, Any]] = []
    checked = 0
    for fact, proof in evaluation.proofs.items():
        if proof.source == "input":
            if fact[1] not in evaluation.input_relations.get(fact[0], set()):
                problems.append({"fact": render_atom(fact), "problem": "input fact not in input"})
            continue
        checked += 1
        rule = evaluation.program.rule(proof.rule_id or "")
        head_fact = instantiate(rule.head, proof.bindings)
        if head_fact != fact:
            problems.append(
                {"fact": render_atom(fact), "problem": "head does not instantiate to proved fact"}
            )
            continue
        # Positive premises.
        premise_indices = [index for index, _ in proof.positive]
        if sorted(premise_indices) != list(range(len(rule.positive))):
            problems.append(
                {
                    "fact": render_atom(fact),
                    "problem": "positive premises do not cover each body atom exactly once",
                }
            )
        for atom_index, premise in proof.positive:
            atom = rule.positive[atom_index]
            expected = instantiate(atom, proof.bindings)
            if expected != premise:
                problems.append(
                    {
                        "fact": render_atom(fact),
                        "problem": f"premise {render_atom(premise)} does not instantiate body atom {atom_index}",
                    }
                )
                continue
            if premise[1] not in evaluation.relations.get(premise[0], set()):
                problems.append(
                    {"fact": render_atom(fact), "problem": f"missing positive premise {render_atom(premise)}"}
                )
                continue
            premise_proof = evaluation.proofs.get(premise)
            if premise_proof is None:
                problems.append(
                    {"fact": render_atom(fact), "problem": f"premise {render_atom(premise)} has no provenance"}
                )
            elif premise_proof.source == "rule":
                if premise_proof.round >= proof.round and premise_proof.stratum == proof.stratum:
                    problems.append(
                        {
                            "fact": render_atom(fact),
                            "problem": f"premise {render_atom(premise)} was derived no earlier than the conclusion",
                        }
                    )
        # Negative atoms.
        if len(proof.negative) != len(rule.negative):
            problems.append(
                {"fact": render_atom(fact), "problem": "negative checks do not match body negative atoms"}
            )
        for atom_index, check in enumerate(proof.negative):
            atom = rule.negative[atom_index]
            expected = instantiate(atom, proof.bindings)
            if expected is None or (check.predicate, check.atom.args) != expected:
                problems.append(
                    {"fact": render_atom(fact), "problem": "negative check does not instantiate body atom"}
                )
                continue
            if check.predicate not in evaluation.program.schema.arities or len(check.atom.args) != evaluation.program.schema.arities[check.predicate]:
                problems.append(
                    {"fact": render_atom(fact), "problem": "negative check has wrong arity"}
                )
                continue
            fact_stratum = evaluation.program.predicate_stratum[check.predicate]
            if fact_stratum >= proof.stratum:
                problems.append(
                    {
                        "fact": render_atom(fact),
                        "problem": f"negative predicate in non-lower stratum {fact_stratum}",
                    }
                )
            if check.atom.args in evaluation.relations.get(check.predicate, set()):
                problems.append(
                    {"fact": render_atom(fact), "problem": f"negative atom {render_atom((check.predicate, check.atom.args))} is present"}
                )
            if not check.missing or check.stratum != fact_stratum or not check.stratum_complete:
                problems.append(
                    {"fact": render_atom(fact), "problem": "negative check record is inconsistent"}
                )
    return {
        "checked_derived_proofs": checked,
        "problems": problems,
        "ok": not problems,
    }


def explain_queries(evaluation: Evaluation, queries: Sequence[Atom]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for atom in queries:
        fact: Fact = (atom.predicate, atom.args)
        present = fact[1] in evaluation.relations.get(fact[0], set())
        entry: dict[str, Any] = {
            "query": atom.to_json(),
            "fact": render_atom(fact),
            "status": "derived" if present else "not_derived",
            "stratum": evaluation.program.predicate_stratum[atom.predicate],
        }
        if present:
            proof = evaluation.proofs.get(fact)
            entry["proof"] = proof.to_json(
                {
                    p: evaluation.proofs[p].source
                    for _, p in (proof.positive if proof else ())
                    if p in evaluation.proofs
                }
            ) if proof else None
            entry["explanation"] = render_proof(evaluation, fact)
        else:
            stratum = evaluation.program.predicate_stratum[atom.predicate]
            stratum_result = next(
                (s for s in evaluation.strata_results if s.index == stratum), None
            )
            text = (
                f"{render_atom(fact)} is not in the completed relation "
                f"{atom.predicate} after stratum {stratum} reached its fixed point"
                + (
                    f" (stable at round {stratum_result.stable_at_round})"
                    if stratum_result is not None
                    else ""
                )
                + "; no positive proof exists."
            )
            entry["saturated"] = True
            entry["explanation"] = [text]
            entry["absent_from_relation"] = True
        results.append(entry)
    return {
        "queries": results,
        "verification": verify_proofs(evaluation),
    }


# ---------------------------------------------------------------------------
# Independent naive full-closure baseline (used for the tiny material)
# ---------------------------------------------------------------------------


def _naive_join(rule: Rule, relations: RelationMap) -> list[dict[str, str]]:
    """Separate, simpler join used only by the oracle.

    Returns every binding satisfying the positive body (constants and
    repeated variables enforced).  No delta, no ordering tricks.
    """
    bindings_list: list[dict[str, str]] = [{}]
    for atom in rule.positive:
        rows = relations.get(atom.predicate, set())
        extended_list: list[dict[str, str]] = []
        for bindings in bindings_list:
            for row in rows:
                if len(row) != len(atom.args):
                    continue
                trial = dict(bindings)
                consistent = True
                for term, value in zip(atom.args, row):
                    if is_variable(term):
                        if term in trial and trial[term] != value:
                            consistent = False
                            break
                        trial[term] = value
                    elif term != value:
                        consistent = False
                        break
                if consistent:
                    extended_list.append(trial)
        bindings_list = extended_list
        if not bindings_list:
            break
    return bindings_list


def naive_full_closure(
    program: Program,
    input_relations: RelationMap,
) -> RelationMap:
    """Independent oracle: naive full recomputation, stratum by stratum.

    It ignores delta bookkeeping and the main evaluator's join entirely.
    Inside each stratum it repeatedly applies *all* of the stratum's rules to
    the entire current relations until nothing changes (the classic naive
    fixed point).  Negation is consulted only after all lower strata have
    reached their own fixed points, so the oracle respects the same
    stratification semantics while sharing none of the semi-naive delta
    logic.  Used only for the tiny material.
    """
    schema = program.schema
    relations: RelationMap = {pred: set() for pred in schema.arities}
    for predicate, rows in input_relations.items():
        relations[predicate] |= set(rows)
    for stratum in program.strata:
        stratum_rules = [program.rule(rule_id) for rule_id in stratum.rules]
        if not stratum_rules:
            continue
        while True:
            changed = False
            for rule in stratum_rules:
                for bindings in _naive_join(rule, relations):
                    head_fact = instantiate(rule.head, bindings)
                    if head_fact is None:
                        continue
                    if head_fact[1] in relations[head_fact[0]]:
                        continue
                    if not rule.negative:
                        negations_hold = True
                    else:
                        negations_hold = True
                        for atom in rule.negative:
                            fact = instantiate(atom, bindings)
                            if fact is None or fact[1] in relations[fact[0]]:
                                negations_hold = False
                                break
                    if not negations_hold:
                        continue
                    relations[head_fact[0]].add(head_fact[1])
                    changed = True
            if not changed:
                break
    return relations


def compare_with_naive(evaluation: Evaluation) -> dict[str, Any]:
    naive = naive_full_closure(evaluation.program, evaluation.input_relations)
    differences: list[dict[str, Any]] = []
    for predicate in sorted(evaluation.program.schema.arities):
        fast = evaluation.relations.get(predicate, set())
        slow = naive.get(predicate, set())
        if fast != slow:
            differences.append(
                {
                    "predicate": predicate,
                    "only_fast": [list(row) for row in sorted(fast - slow)],
                    "only_naive": [list(row) for row in sorted(slow - fast)],
                }
            )
    return {
        "ok": not differences,
        "differences": differences,
        "naive_counts": {p: len(rows) for p, rows in sorted(naive.items())},
        "semi_naive_counts": {
            p: len(evaluation.relations.get(p, set()))
            for p in sorted(evaluation.program.schema.arities)
        },
    }


# ---------------------------------------------------------------------------
# Input loading / command line
# ---------------------------------------------------------------------------


@dataclass
class LoadedInputs:
    schema: Schema | None
    rules: tuple[Rule, ...]
    program: Program | None
    facts: RelationMap | None
    fact_counts: dict[Fact, int]
    duplicates: int
    queries: tuple[Atom, ...]
    issues: list[Issue]


def load_inputs(
    schema_path: Path,
    rules_path: Path,
    facts_path: Path | None,
    queries_path: Path | None,
) -> LoadedInputs:
    issues: list[Issue] = []
    schema = parse_schema(load_json_file(schema_path, issues, "schema"), issues)
    rules_raw = load_json_file(rules_path, issues, "rules")
    rules: tuple[Rule, ...] = ()
    program: Program | None = None
    if schema is not None and rules_raw is not None:
        rules = parse_rules(rules_raw, schema, issues)
        if rules or not any(i.code == "rules.shape" for i in issues):
            program = build_program(schema, rules, issues)
    facts: RelationMap | None = None
    fact_counts: dict[Fact, int] = {}
    duplicates = 0
    if facts_path is not None and schema is not None:
        facts, fact_counts, duplicates = parse_facts(facts_path, schema, issues)
    queries: tuple[Atom, ...] = ()
    if queries_path is not None and schema is not None:
        queries = parse_queries(load_json_file(queries_path, issues, "queries"), schema, issues)
    return LoadedInputs(
        schema=schema,
        rules=rules,
        program=program,
        facts=facts,
        fact_counts=fact_counts,
        duplicates=duplicates,
        queries=queries,
        issues=issues,
    )


def issue_response(status: str, issues: Sequence[Issue], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"status": status}
    if extra:
        out.update(extra)
    out["errors"] = [issue.to_json() for issue in issues]
    out["ok"] = False
    return out


def emit(payload: dict[str, Any], out_path: Path | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if out_path is not None:
        out_path.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def evaluate_loaded(loaded: LoadedInputs) -> Evaluation:
    assert loaded.program is not None and loaded.facts is not None
    return evaluate(loaded.program, loaded.facts, loaded.fact_counts, loaded.duplicates)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stratified Datalog check/derive/explain over small fixtures."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, facts_required: bool = True) -> None:
        p.add_argument("--schema", type=Path, required=True)
        p.add_argument("--rules", type=Path, required=True)
        p.add_argument("--facts", type=Path, required=facts_required)
        p.add_argument("--queries", type=Path, default=None)
        p.add_argument("--out", type=Path, default=None)
        p.add_argument(
            "--no-proofs",
            action="store_true",
            help="omit the full proof map from derive output",
        )

    check = sub.add_parser("check", help="validate schema, rules and facts without evaluating")
    common(check, facts_required=False)

    derive = sub.add_parser("derive", help="evaluate the program and report relations/deltas")
    common(derive)

    explain = sub.add_parser("explain", help="evaluate and explain the given queries")
    common(explain)

    tiny = sub.add_parser(
        "verify-tiny",
        help="cross-check semi-naive evaluation against a naive full closure",
    )
    common(tiny)
    return parser


def run_check(args: argparse.Namespace) -> dict[str, Any]:
    loaded = load_inputs(args.schema, args.rules, args.facts, args.queries)
    if loaded.issues or loaded.program is None:
        return issue_response(
            "invalid",
            loaded.issues,
            {
                "schema": loaded.schema.to_json() if loaded.schema else None,
                "facts_loaded": (
                    sum(len(rows) for rows in loaded.facts.values())
                    if loaded.facts is not None
                    else None
                ),
            },
        )
    payload: dict[str, Any] = {
        "status": "ok",
        "ok": True,
        "schema": loaded.schema.to_json() if loaded.schema else None,
        "program": loaded.program.to_json(),
        "input": {
            "fact_counts": {
                pred: len(rows) for pred, rows in sorted((loaded.facts or {}).items())
            },
            "total": sum(len(rows) for rows in (loaded.facts or {}).values()),
            "duplicates_removed": loaded.duplicates,
        },
        "queries": [q.to_json() for q in loaded.queries],
        "warnings": [w.to_json() for w in loaded.program.warnings],
        "errors": [],
    }
    return payload


def load_and_program(args: argparse.Namespace) -> tuple[LoadedInputs, Program | None, dict[str, Any] | None]:
    loaded = load_inputs(args.schema, args.rules, args.facts, args.queries)
    if loaded.issues or loaded.program is None or loaded.facts is None:
        return loaded, None, issue_response("invalid", loaded.issues)
    return loaded, loaded.program, None


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "check":
        payload = run_check(args)
        emit(payload, args.out)
        return 0 if payload.get("ok") else 1

    loaded, program, failure = load_and_program(args)
    if failure is not None:
        emit(failure, args.out)
        return 1
    assert program is not None and loaded.facts is not None

    evaluation = evaluate(loaded.program, loaded.facts, loaded.fact_counts, loaded.duplicates)

    if args.command == "derive":
        payload = evaluation.to_json(include_proofs=not args.no_proofs)
        if loaded.queries:
            payload["queries"] = explain_queries(evaluation, loaded.queries)["queries"]
        payload["proof_verification"] = verify_proofs(evaluation)
        payload["ok"] = True
        emit(payload, args.out)
        return 0

    if args.command == "explain":
        explained = explain_queries(evaluation, loaded.queries)
        payload = {
            "status": "ok",
            "ok": True,
            "program": program.to_json(),
            "input": {
                "fact_counts": {
                    pred: len(rows) for pred, rows in sorted(loaded.facts.items())
                },
                "duplicates_removed": loaded.duplicates,
            },
            "relations": {
                pred: [list(args_) for args_ in sorted(rows)]
                for pred, rows in sorted(evaluation.relations.items())
            },
            "fact_counts": {
                pred: len(rows) for pred, rows in sorted(evaluation.relations.items())
            },
            "strata": [s.to_json() for s in evaluation.strata_results],
            "queries": explained["queries"],
            "verification": explained["verification"],
            "errors": [],
        }
        if not loaded.queries:
            payload["note"] = "no queries were supplied; nothing to explain"
        emit(payload, args.out)
        return 0

    if args.command == "verify-tiny":
        comparison = compare_with_naive(evaluation)
        verification = verify_proofs(evaluation)
        payload = {
            "status": "ok" if comparison["ok"] and verification["ok"] else "mismatch",
            "ok": comparison["ok"] and verification["ok"],
            "program": program.to_json(),
            "naive_vs_semi_naive": comparison,
            "proof_verification": verification,
            "strata": [s.to_json() for s in evaluation.strata_results],
            "relations": {
                pred: [list(args_) for args_ in sorted(rows)]
                for pred, rows in sorted(evaluation.relations.items())
            },
            "errors": [],
        }
        emit(payload, args.out)
        return 0 if payload["ok"] else 1

    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
