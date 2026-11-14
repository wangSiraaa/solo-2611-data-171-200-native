"""Parameter-cell rewriting.

A parameters cell is a code cell tagged ``parameters`` whose body consists
only of simple ``name = literal`` assignments (the fixture contract). JSON
scalar values from the override file may only replace names that already
exist in that cell; unknown names are rejected so an override can never
inject arbitrary code.
"""

from __future__ import annotations

import ast
from typing import Any, Dict, List, Tuple

JSON_SCALARS = (str, int, float, bool, type(None))


class ParameterOverrideError(ValueError):
    """Raised when a parameter override cannot be applied safely."""


def find_parameters_cell(notebook) -> Tuple[int, Any]:
    """Return (index, cell) of the unique parameters-tagged code cell."""
    matches = [
        (idx, cell)
        for idx, cell in enumerate(notebook.cells)
        if cell.cell_type == "code" and "parameters" in cell.get("metadata", {}).get("tags", [])
    ]
    if not matches:
        raise ParameterOverrideError("no code cell tagged 'parameters' was found")
    if len(matches) > 1:
        raise ParameterOverrideError(
            f"expected exactly one parameters cell, found {len(matches)}"
        )
    return matches[0]


def _extract_simple_assignments(source: str) -> Dict[str, ast.Assign]:
    tree = ast.parse(source)
    names: Dict[str, ast.Assign] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            raise ParameterOverrideError(
                "parameters cell may only contain simple name=literal assignments; "
                f"found {type(node).__name__}"
            )
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            raise ParameterOverrideError(
                "parameters cell may only assign to a single plain name"
            )
        if not isinstance(node.value, ast.Constant) or not isinstance(
            node.value.value, JSON_SCALARS
        ):
            raise ParameterOverrideError(
                f"parameter {node.targets[0].id!r} must be a JSON scalar literal"
            )
        names[node.targets[0].id] = node
    return names


def apply_parameters(notebook, overrides: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rewrite the parameters cell in-place using JSON scalar overrides.

    Only names already present as simple assignments may be overridden.
    Returns a list describing every applied replacement.
    """
    index, cell = find_parameters_cell(notebook)
    original_source = cell.source
    assignments = _extract_simple_assignments(original_source)

    unknown = sorted(set(overrides) - set(assignments))
    if unknown:
        raise ParameterOverrideError(
            f"override names not present in the parameters cell: {unknown}"
        )
    bad = {k: type(v).__name__ for k, v in overrides.items() if not isinstance(v, JSON_SCALARS)}
    if bad:
        raise ParameterOverrideError(f"only JSON scalar values are supported, got {bad}")

    changes: List[Dict[str, Any]] = []
    for name, node in assignments.items():
        if name not in overrides:
            continue
        old_value = node.value.value
        new_value = overrides[name]
        node.value = ast.Constant(value=new_value)
        changes.append(
            {
                "name": name,
                "old_value": old_value,
                "new_value": new_value,
            }
        )

    tree = ast.parse(original_source)
    for stmt in tree.body:
        target = stmt.targets[0].id
        if target in overrides:
            stmt.value = ast.Constant(value=overrides[target])
    ast.fix_missing_locations(tree)
    new_source = ast.unparse(tree)
    cell.source = new_source
    return changes
