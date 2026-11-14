"""Global, backtracking dependency solver.

Properties enforced:
  * exactly one version per normalized package name (a global assignment, not
    per-requirement satisfaction);
  * extras are additive: a package first pulled in normally can later gain an
    extra from any other dependency, and the extra's dependencies are activated
    until the closure reaches a fixpoint;
  * ``Requires-Python`` filters candidates against the target interpreter;
  * markers are evaluated against the target environment;
  * candidate order is deterministic (newest first), independent of how the
    wheelhouse directory is enumerated;
  * conflicts keep the requirement edge chain that introduced each constraint.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Iterable

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

from .target import validate_marker_environment
from .wheels import Candidate, Diagnostic, WheelIndex


@dataclass(frozen=True)
class Edge:
    """One active requirement edge (root or parent wheel -> child)."""

    parent: str | None  # None for a root requirement
    parent_version: str | None
    parent_extras: frozenset[str]
    raw: str
    target: str
    specifier: SpecifierSet
    extras: frozenset[str]
    marker: str | None
    edge_id: tuple[str, str, str]

    @property
    def source(self) -> str:
        if self.parent is None:
            return "root"
        extras = ",".join(sorted(self.parent_extras))
        return f"{self.parent} {self.parent_version}" + (
            f" [{extras}]" if extras else ""
        )


@dataclass(frozen=True)
class Constraint:
    name: str
    specifier: SpecifierSet
    extras: frozenset[str]
    marker: str | None
    raw: str
    edge: Edge


@dataclass
class State:
    selected: dict[str, Candidate] = field(default_factory=dict)
    extras: dict[str, frozenset[str]] = field(default_factory=dict)
    constraints: dict[str, list[Constraint]] = field(default_factory=dict)
    activated: frozenset[tuple[str, str, str]] = frozenset()
    pending: list[Constraint] = field(default_factory=list)
    introduced_by: dict[str, Edge] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)

    def clone(self) -> "State":
        return State(
            selected=dict(self.selected),
            extras=dict(self.extras),
            constraints={k: list(v) for k, v in self.constraints.items()},
            activated=self.activated,
            pending=list(self.pending),
            introduced_by=dict(self.introduced_by),
            edges=list(self.edges),
        )


@dataclass
class Conflict:
    kind: str
    package: str
    detail: str
    chain: list[str]
    constraints: list[dict[str, object]]
    candidates: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "package": self.package,
            "detail": self.detail,
            "requirement_chain": self.chain,
            "constraints": self.constraints,
            "candidates": self.candidates,
        }


@dataclass
class Solution:
    selected: dict[str, Candidate]
    extras: dict[str, frozenset[str]]
    constraints: dict[str, list[Constraint]]
    edges: list[Edge]
    introduced_by: dict[str, Edge]


class RootRequirementError(ValueError):
    def __init__(self, diagnostics: list[Diagnostic]):
        self.diagnostics = diagnostics
        super().__init__("; ".join(str(d) for d in diagnostics))


def _chain_for(state: State, edge: Edge) -> list[str]:
    """Readable root-to-edge requirement chain."""
    chain: list[str] = []
    current: Edge | None = edge
    guard = 0
    while current is not None and guard < 64:
        guard += 1
        if current.parent is None:
            chain.append(f"root requires {current.raw}")
            break
        chain.append(f"{current.source} requires {current.raw}")
        current = state.introduced_by.get(current.parent)
    if current is None and guard < 64:
        chain.append("<origin edge unavailable>")
    return list(reversed(chain))


class Solver:
    def __init__(self, index: WheelIndex, environment: dict[str, str], target_python: str):
        self.index = index
        self.environment = environment
        self.target_python = target_python
        # Candidates whose dependency markers cannot be evaluated in this target
        # environment; they must never be selected.
        self.invalid_candidates: set[tuple[str, str]] = set()

    # -- root requirement parsing -------------------------------------------------
    def parse_root_requirements(
        self, requirements: Iterable[str]
    ) -> tuple[list[Constraint], list[Diagnostic]]:
        constraints: list[Constraint] = []
        diagnostics: list[Diagnostic] = []
        for idx, raw in enumerate(requirements):
            try:
                req = Requirement(raw)
            except InvalidRequirement as exc:
                diagnostics.append(
                    Diagnostic(
                        "invalid-requirement", f"root[{idx}]", f"{raw!r}: {exc}"
                    )
                )
                continue
            if req.url is not None:
                diagnostics.append(
                    Diagnostic(
                        "direct-url",
                        f"root[{idx}]",
                        f"direct URL requirement is unsupported: {raw!r}",
                    )
                )
                continue
            marker_error = validate_marker_environment(
                str(req.marker) if req.marker else None, self.environment, f"root[{idx}]"
            )
            if marker_error:
                diagnostics.append(
                    Diagnostic("invalid-marker", f"root[{idx}]", marker_error)
                )
                continue
            if req.marker is not None and not req.marker.evaluate(
                {**self.environment, "extra": ""}
            ):
                # Root markers that do not hold contribute no constraint.
                continue
            edge = Edge(
                parent=None,
                parent_version=None,
                parent_extras=frozenset(),
                raw=raw,
                target=canonicalize_name(req.name),
                specifier=req.specifier,
                extras=frozenset(canonicalize_name(e) for e in req.extras),
                marker=str(req.marker) if req.marker else None,
                edge_id=("root", str(idx), raw),
            )
            constraints.append(
                Constraint(
                    name=canonicalize_name(req.name),
                    specifier=req.specifier,
                    extras=frozenset(canonicalize_name(e) for e in req.extras),
                    marker=str(req.marker) if req.marker else None,
                    raw=raw,
                    edge=edge,
                )
            )
        return constraints, diagnostics

    # -- candidate scanning -------------------------------------------------------
    def validate_candidates(self) -> list[Diagnostic]:
        """Flag candidates with markers referencing unknown env variables.

        The index itself is not mutated; flagged candidates are recorded in
        ``invalid_candidates`` and excluded by :meth:`_usable_versions`.
        """
        diagnostics: list[Diagnostic] = []
        for name in self.index.names():
            for candidate in self.index.versions(name):
                for dep in candidate.dependencies:
                    err = validate_marker_environment(
                        dep.marker, self.environment, candidate.filename
                    )
                    if err:
                        diagnostics.append(
                            Diagnostic("invalid-marker", candidate.filename, err)
                        )
                        self.invalid_candidates.add(
                            (candidate.name, str(candidate.version))
                        )
                        break
        return diagnostics

    def _usable_versions(self, name: str) -> list[Candidate]:
        return [
            c
            for c in self.index.versions(name)
            if (c.name, str(c.version)) not in self.invalid_candidates
        ]

    def _python_ok(self, candidate: Candidate) -> bool:
        return candidate.python_ok(
            self.environment["python_full_version"],
            self.environment["python_version"],
        )

    def _enqueue_dependencies(
        self, state: State, candidate: Candidate, extras: frozenset[str]
    ) -> None:
        for dep in candidate.dependencies:
            if not dep.applies_with(extras, self.environment):
                continue
            edge_id = (candidate.name, str(candidate.version), dep.raw)
            if edge_id in state.activated:
                continue
            edge = Edge(
                parent=candidate.name,
                parent_version=str(candidate.version),
                parent_extras=extras,
                raw=dep.raw,
                target=dep.name,
                specifier=dep.specifier,
                extras=dep.extras,
                marker=dep.marker,
                edge_id=edge_id,
            )
            state.activated = state.activated | {edge_id}
            state.edges.append(edge)
            state.pending.append(
                Constraint(
                    name=dep.name,
                    specifier=dep.specifier,
                    extras=dep.extras,
                    marker=dep.marker,
                    raw=dep.raw,
                    edge=edge,
                )
            )

    # -- conflict helpers ------------------------------------------------------------
    def _constraint_dict(self, state: State, constraint: Constraint) -> dict[str, object]:
        return {
            "requirement": constraint.raw,
            "package": constraint.name,
            "specifier": str(constraint.specifier),
            "requested_extras": sorted(constraint.extras),
            "source": constraint.edge.source,
            "requirement_chain": _chain_for(state, constraint.edge),
        }

    def _version_conflict(
        self, state: State, constraint: Constraint, selected: Candidate
    ) -> Conflict:
        constraints = list(state.constraints.get(constraint.name, []))
        others = [c for c in constraints if c is not constraint]
        detail = (
            f"selected {selected.name} {selected.version} (from "
            f"{_chain_for(state, state.introduced_by[selected.name])[-1]}) "
            f"does not satisfy {constraint.specifier} required by "
            f"{constraint.raw!r} from {constraint.edge.source}"
        )
        involved = others + [constraint]
        return Conflict(
            kind="version-conflict",
            package=constraint.name,
            detail=detail,
            chain=_chain_for(state, constraint.edge),
            constraints=[self._constraint_dict(state, c) for c in involved],
        )

    def _no_candidate_conflict(
        self, state: State, name: str, constraints: list[Constraint]
    ) -> Conflict:
        candidates = self.index.versions(name)
        candidate_reports: list[dict[str, object]] = []
        for cand in candidates:
            reasons: list[str] = []
            missing_extras = set()
            for c in constraints:
                missing_extras |= set(c.extras)
            missing_extras -= cand.provides_extras
            if missing_extras:
                reasons.append(
                    f"does not provide requested extra(s) {sorted(missing_extras)}"
                )
            if (cand.name, str(cand.version)) in self.invalid_candidates:
                reasons.append(
                    "dependency markers reference variables absent from the "
                    "target environment"
                )
            if not self._python_ok(cand):
                reasons.append(
                    "Requires-Python "
                    f"{cand.requires_python_display} excludes target "
                    f"{self.environment['python_full_version']}"
                )
            for c in constraints:
                if not c.specifier.contains(cand.version):
                    reasons.append(
                        f"violates {c.specifier} from {c.raw!r} "
                        f"({c.edge.source})"
                    )
            candidate_reports.append(
                {
                    "candidate": f"{cand.name} {cand.version}",
                    "wheel": cand.filename,
                    "requires_python": cand.requires_python_display,
                    "rejected_because": reasons,
                }
            )
        if not candidates:
            detail = f"no wheel in the wheelhouse provides {name}"
        else:
            detail = (
                f"no version of {name} satisfies every active requirement and "
                f"the target Python {self.environment['python_full_version']}"
            )
        chains = []
        for c in constraints:
            for line in _chain_for(state, c.edge):
                if line not in chains:
                    chains.append(line)
        return Conflict(
            kind="no-viable-candidate",
            package=name,
            detail=detail,
            chain=chains,
            constraints=[self._constraint_dict(state, c) for c in constraints],
            candidates=candidate_reports,
        )

    # -- core search ------------------------------------------------------------------
    def _process(self, state: State) -> tuple[Solution | None, list[Conflict]]:
        if not state.pending:
            return self._finish(state)
        constraint = state.pending[0]
        state.pending = state.pending[1:]

        # Record the constraint and accumulate requested extras.
        state.constraints.setdefault(constraint.name, []).append(constraint)
        previous_extras = state.extras.get(constraint.name, frozenset())
        new_extras = previous_extras | constraint.extras
        state.extras[constraint.name] = new_extras

        selected = state.selected.get(constraint.name)
        if selected is not None:
            if not constraint.specifier.contains(selected.version):
                return None, [self._version_conflict(state, constraint, selected)]
            missing = new_extras - selected.provides_extras
            if missing:
                return None, [
                    Conflict(
                        kind="unknown-extra",
                        package=constraint.name,
                        detail=(
                            f"{constraint.name} {selected.version} does not provide "
                            f"extra(s) {sorted(missing)} requested by "
                            f"{constraint.raw!r} from {constraint.edge.source}"
                        ),
                        chain=_chain_for(state, constraint.edge),
                        constraints=[self._constraint_dict(state, constraint)],
                    )
                ]
            if new_extras != previous_extras:
                # A late extra reactivates dependencies of the selected wheel.
                self._enqueue_dependencies(state, selected, new_extras)
            return self._process(state)

        # Not selected yet: solve for a globally consistent candidate.
        all_constraints = list(state.constraints[constraint.name])
        viable = [
            cand
            for cand in self._usable_versions(constraint.name)
            if self._python_ok(cand)
            and new_extras <= cand.provides_extras
            and all(c.specifier.contains(cand.version) for c in all_constraints)
        ]
        if not viable:
            return None, [
                self._no_candidate_conflict(state, constraint.name, all_constraints)
            ]

        conflicts: list[Conflict] = []
        for candidate in viable:
            branch = state.clone()
            branch.selected[constraint.name] = candidate
            branch.introduced_by[constraint.name] = constraint.edge
            branch.extras[constraint.name] = new_extras
            self._enqueue_dependencies(branch, candidate, new_extras)
            solution, branch_conflicts = self._process(branch)
            if solution is not None:
                return solution, []
            conflicts.extend(branch_conflicts)
        return None, conflicts

    def _finish(self, state: State) -> tuple[Solution | None, list[Conflict]]:
        """Final closure check: stable extras, all constraints satisfied."""
        conflicts: list[Conflict] = []
        for name, candidate in sorted(state.selected.items()):
            active = state.extras.get(name, frozenset())
            for dep in candidate.dependencies:
                if not dep.applies_with(active, self.environment):
                    continue
                edge_id = (candidate.name, str(candidate.version), dep.raw)
                if edge_id not in state.activated:
                    conflicts.append(
                        Conflict(
                            kind="unstable-closure",
                            package=name,
                            detail=f"dependency {dep.raw!r} was never activated",
                            chain=[],
                            constraints=[],
                        )
                    )
            for constraint in state.constraints.get(name, []):
                if not constraint.specifier.contains(candidate.version):
                    conflicts.append(
                        self._version_conflict(state, constraint, candidate)
                    )
        if conflicts:
            return None, conflicts
        return Solution(
            selected=dict(state.selected),
            extras=dict(state.extras),
            constraints={k: list(v) for k, v in state.constraints.items()},
            edges=list(state.edges),
            introduced_by=dict(state.introduced_by),
        ), []

    def solve(self, constraints: list[Constraint]) -> tuple[Solution | None, list[Conflict]]:
        initial = State()
        initial.pending = list(constraints)
        # Root requirement edges participate in provenance just like wheel
        # dependency edges (parent is None).
        initial.edges = [c.edge for c in constraints]
        # The search recurses once per pending constraint; give deep but finite
        # dependency graphs enough headroom without masking real bugs.
        old_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(old_limit, 20000))
        try:
            return self._process(initial)
        finally:
            sys.setrecursionlimit(old_limit)


def dedupe_conflicts(conflicts: list[Conflict], limit: int = 200) -> list[Conflict]:
    seen: set[str] = set()
    out: list[Conflict] = []
    for conflict in conflicts:
        key = repr(
            (
                conflict.kind,
                conflict.package,
                conflict.detail,
                tuple(
                    (c["requirement"], c["source"]) for c in conflict.constraints
                ),
            )
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(conflict)
        if len(out) >= limit:
            break
    return out
