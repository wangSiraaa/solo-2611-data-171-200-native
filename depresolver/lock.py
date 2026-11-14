"""Lock-file and resolution-report construction."""

from __future__ import annotations

from typing import Any

from .solver import Conflict, Solution, _chain_for
from .wheels import Diagnostic, WheelIndex


def build_lock(
    case_id: str,
    solution: Solution,
    environment: dict[str, str],
    root_requirements: list[str],
) -> dict[str, Any]:
    """Turn a solution into a deterministic, auditable lock document."""
    packages: list[dict[str, Any]] = []
    for name in sorted(solution.selected):
        candidate = solution.selected[name]
        extras = solution.extras.get(name, frozenset())
        incoming = [e for e in solution.edges if e.target == name]
        reason_edges = []
        for edge in sorted(
            incoming,
            key=lambda e: (e.parent or "", e.raw, ",".join(sorted(e.parent_extras))),
        ):
            reason_edges.append(
                {
                    "requested_by": edge.source,
                    "requirement": edge.raw,
                    "specifier": str(edge.specifier),
                    "requested_extras": sorted(edge.extras),
                    "marker": edge.marker,
                    "chain": _chain_for(_state_proxy(solution), edge),
                }
            )
        outgoing = [
            {
                "requirement": e.raw,
                "target": e.target,
                "target_extras": sorted(e.extras),
                "marker": e.marker,
                "active_because_parent_extras": sorted(e.parent_extras),
            }
            for e in sorted(solution.edges, key=lambda e: e.raw)
            if e.parent == name
        ]
        packages.append(
            {
                "name": name,
                "version": str(candidate.version),
                "wheel": candidate.filename,
                "wheel_path": candidate.path,
                "sha256": candidate.sha256,
                "active_extras": sorted(extras),
                "requires_python": candidate.requires_python_display,
                "top_level_modules": list(candidate.top_level),
                "reason_edges": reason_edges,
                "dependencies_emitted": outgoing,
            }
        )

    return {
        "lock_version": 1,
        "case_id": case_id,
        "root_requirements": root_requirements,
        "target_environment": environment,
        "one_version_per_normalized_name": True,
        "packages": packages,
    }


class _StateProxy:
    """Minimal adapter so ``_chain_for`` can walk solution edges."""

    def __init__(self, solution: Solution):
        self.introduced_by = solution.introduced_by


def _state_proxy(solution: Solution) -> _StateProxy:
    return _StateProxy(solution)


def build_report(
    case_id: str,
    root_requirements: list[str],
    environment: dict[str, str],
    diagnostics: list[Diagnostic],
    solution: Solution | None,
    conflicts: list[Conflict],
    index: WheelIndex,
    wheelhouse_diagnostics: list[Diagnostic] | None = None,
    input_errors: list[Diagnostic] | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "case_id": case_id,
        "root_requirements": root_requirements,
        "target_python": environment.get("python_full_version"),
        "status": "solvable" if solution is not None else "unsolvable",
        "pre_solve_diagnostics": [d.as_dict() for d in diagnostics],
        "wheelhouse_diagnostics": [
            d.as_dict() for d in (wheelhouse_diagnostics or [])
        ],
        "input_errors": [d.as_dict() for d in (input_errors or [])],
        "wheelhouse": {
            name: [
                {
                    "version": str(c.version),
                    "wheel": c.filename,
                    "sha256": c.sha256,
                    "requires_python": c.requires_python_display,
                }
                for c in index.versions(name)
            ]
            for name in index.names()
        },
    }
    if solution is not None:
        checks: list[dict[str, Any]] = []
        # Independently re-check every active constraint against the selection.
        for name in sorted(solution.constraints):
            for constraint in solution.constraints[name]:
                selected = solution.selected.get(name)
                satisfied = (
                    selected is not None
                    and constraint.specifier.contains(selected.version)
                )
                checks.append(
                    {
                        "requirement": constraint.raw,
                        "package": name,
                        "specifier": str(constraint.specifier),
                        "source": constraint.edge.source,
                        "selected": (
                            f"{selected.name} {selected.version}"
                            if selected
                            else None
                        ),
                        "satisfied": satisfied,
                        "chain": _chain_for(_state_proxy(solution), constraint.edge),
                    }
                )
        report["constraint_checks"] = checks
        report["all_constraints_satisfied"] = all(
            c["satisfied"] for c in checks
        )
        extra_checks = []
        for name, candidate in sorted(solution.selected.items()):
            active = solution.extras.get(name, frozenset())
            for extra in sorted(active):
                extra_checks.append(
                    {
                        "package": name,
                        "active_extra": extra,
                        "provided_by_wheel": extra in candidate.provides_extras,
                    }
                )
        report["extra_checks"] = extra_checks
        report["all_extras_provided"] = all(
            e["provided_by_wheel"] for e in extra_checks
        )
        report["closure"] = [
            {
                "package": name,
                "version": str(candidate.version),
                "active_extras": sorted(solution.extras.get(name, frozenset())),
                "emitted_requirements": [
                    e.raw
                    for e in solution.edges
                    if e.parent == name
                ],
            }
            for name, candidate in sorted(solution.selected.items())
        ]
    else:
        report["conflicts"] = [c.as_dict() for c in conflicts]
        # A compact, deduplicated view naming every parent/source that placed a
        # mutually incompatible constraint on the contested package.
        summary: dict[str, dict[str, Any]] = {}
        for conflict in conflicts:
            entry = summary.setdefault(
                conflict.package, {"constraints": [], "requirement_chains": []}
            )
            for constraint in conflict.constraints:
                item = {
                    "requirement": constraint.get("requirement"),
                    "specifier": constraint.get("specifier"),
                    "source": constraint.get("source"),
                }
                if item not in entry["constraints"]:
                    entry["constraints"].append(item)
            for line in conflict.chain:
                if line not in entry["requirement_chains"]:
                    entry["requirement_chains"].append(line)
        report["conflict_summary"] = summary
    return report
