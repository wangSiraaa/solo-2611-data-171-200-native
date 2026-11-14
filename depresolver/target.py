"""Target build environment handling for marker evaluation.

Markers must be interpreted against the environment the wheels will be
installed into, not against the host running the resolver.
"""

from __future__ import annotations

import json
from typing import Any

from packaging.markers import Marker, Variable
from packaging.version import InvalidVersion, Version

# Keys required by PEP 508 (``extra`` is supplied per requirement, separately).
REQUIRED_KEYS = {
    "implementation_name",
    "implementation_version",
    "os_name",
    "platform_machine",
    "platform_python_implementation",
    "platform_release",
    "platform_system",
    "platform_version",
    "python_full_version",
    "python_version",
    "sys_platform",
}


class TargetEnvironmentError(ValueError):
    """The supplied target environment cannot be used for marker evaluation."""


def load_target_environment(path: str) -> dict[str, str]:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise TargetEnvironmentError("target environment must be a JSON object")
    missing = sorted(REQUIRED_KEYS - set(raw))
    if missing:
        raise TargetEnvironmentError(
            "target environment is missing keys: " + ", ".join(missing)
        )
    env: dict[str, str] = {}
    for key in REQUIRED_KEYS:
        value = raw[key]
        if not isinstance(value, str):
            raise TargetEnvironmentError(f"target environment key {key!r} is not a string")
        env[key] = value
    for key in ("python_version", "python_full_version", "implementation_version"):
        try:
            Version(env[key])
        except InvalidVersion as exc:
            raise TargetEnvironmentError(
                f"target environment key {key!r} is not a valid PEP 440 version: {exc}"
            ) from exc
    return env


def marker_variables(marker: Marker | str | None) -> set[str]:
    """Return all environment variable names referenced by a marker."""
    if marker is None:
        return set()
    if isinstance(marker, str):
        marker = Marker(marker)

    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if isinstance(node, tuple):
            left = node[0]
            if isinstance(left, Variable):
                found.add(left.value)
            return

    walk(marker._markers)
    return found


def validate_marker_environment(
    marker: str | None, environment: dict[str, str], subject: str
) -> str | None:
    """Return an error string when a marker needs a variable we do not know."""
    if marker is None:
        return None
    try:
        refs = marker_variables(marker)
    except Exception as exc:  # malformed marker
        return f"{subject}: cannot parse marker {marker!r}: {exc}"
    unknown = sorted(refs - set(environment) - {"extra"})
    if unknown:
        return (
            f"{subject}: marker {marker!r} references variables absent from the "
            f"target environment: {', '.join(unknown)}"
        )
    return None
