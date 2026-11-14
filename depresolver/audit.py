"""Independent audit of a lock file.

This deliberately does not reuse the solver's internal state.  It re-reads the
wheels, re-evaluates every marker against the target environment recorded in the
lock, recomputes the transitive closure from the root requirements with the
locked active extras, and checks every selected version against every active
requirement.  It is the "independent check" that accompanies the solver.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from .wheels import load_wheelhouse


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _marker_active(marker: str | None, extras: set[str], env: dict[str, str]) -> bool:
    if marker is None:
        return True
    parsed = Marker(marker)
    contexts = sorted(extras) or [""]
    return any(parsed.evaluate({**env, "extra": e}) for e in contexts)


def audit_lock(lock: dict[str, Any], wheelhouse: str | None = None) -> dict[str, Any]:
    env = lock["target_environment"]
    packages = {p["name"]: p for p in lock["packages"]}
    errors: list[str] = []
    checks: list[dict[str, Any]] = []

    # 1. One version per normalized name; identities are normalized.
    if len(packages) != len(lock["packages"]):
        errors.append("duplicate normalized package name in lock")
    for name in packages:
        if name != canonicalize_name(name):
            errors.append(f"lock name {name!r} is not canonicalized")

    # 2. Wheel files exist and match the locked SHA256.
    index = None
    if wheelhouse:
        index = load_wheelhouse(wheelhouse)
    for name, package in packages.items():
        wheel = package.get("wheel_path")
        if not wheel or not os.path.isfile(wheel):
            errors.append(f"{name}: wheel file missing ({wheel})")
            continue
        actual = _sha256(wheel)
        if actual != package["sha256"]:
            errors.append(f"{name}: SHA256 mismatch")

    # 3. Recompute the closure from roots + declared extras alone (do not trust
    # the lock's active_extras), iterating to a fixpoint.
    required: dict[str, list[tuple[str, Requirement, str]]] = {}
    derived_extras: dict[str, set[str]] = {}
    # (package, extra-set snapshot) that still need their dependencies scanned.
    queue: list[str] = []
    queued_state: dict[str, set[str] | None] = {}

    def add_requirement(raw_req: str, source: str) -> str:
        req = Requirement(raw_req)
        name = canonicalize_name(req.name)
        required.setdefault(name, []).append((raw_req, req, source))
        was_known = name in derived_extras
        merged = derived_extras.get(name, set()) | {
            canonicalize_name(e) for e in req.extras
        }
        if not was_known or merged != derived_extras.get(name, set()):
            derived_extras[name] = merged
            queue.append(name)
            queued_state.pop(name, None)
        return name

    for raw in lock["root_requirements"]:
        add_requirement(raw, "root")

    while queue:
        name = queue.pop(0)
        if name not in packages:
            continue  # already reported by the caller below
        package = packages[name]
        candidate = None
        if index is not None:
            for c in index.versions(name):
                if str(c.version) == package["version"]:
                    candidate = c
                    break
        if candidate is None:
            errors.append(
                f"{name}: locked wheel {package['wheel']} not found in wheelhouse"
            )
            continue
        extras_now = frozenset(derived_extras.get(name, set()))
        previously_scanned = queued_state.get(name, "<unset>")
        if previously_scanned != "<unset>" and previously_scanned == extras_now:
            continue  # already scanned with this exact extra set
        queued_state[name] = set(extras_now)
        for dep in candidate.dependencies:
            if not _marker_active(dep.marker, set(extras_now), env):
                continue
            dep_name = add_requirement(dep.raw, f"{name} {package['version']}")
            if dep_name not in packages:
                errors.append(
                    f"{name} {package['version']} requires {dep.raw!r} "
                    f"but {dep_name} is not in the lock"
                )

    # Detect extras that differ from what the lock recorded.
    for name, package in packages.items():
        declared = set(package.get("active_extras", []))
        derived = derived_extras.get(name, set())
        if derived != declared:
            errors.append(
                f"{name}: lock active_extras {sorted(declared)} != re-derived "
                f"{sorted(derived)}"
            )
    active_extras = {name: set(values) for name, values in derived_extras.items()}
    # 4. Every active requirement must be satisfied by the locked version.
    for name in sorted(required):
        if name not in packages:
            errors.append(f"required package {name} is not locked")
            continue
        selected = Version(packages[name]["version"])
        for raw, req, source in required[name]:
            ok = req.specifier.contains(selected)
            checks.append(
                {
                    "package": name,
                    "requirement": raw,
                    "source": source,
                    "selected": str(selected),
                    "satisfied": ok,
                    "requested_extras": sorted(req.extras),
                }
            )
            if not ok:
                errors.append(
                    f"{name} {selected} does not satisfy {raw!r} from {source}"
                )
            for extra in req.extras:
                if extra not in active_extras.get(name, set()):
                    errors.append(
                        f"{name}: extra {extra!r} requested by {source} is not "
                        "active in the lock"
                    )

    # 5. Locked active extras must be provided by the selected wheel metadata.
    if index is not None:
        for name, package in packages.items():
            provided = set()
            for c in index.versions(name):
                if str(c.version) == package["version"]:
                    provided = set(c.provides_extras)
                    break
            for extra in active_extras.get(name, set()):
                if extra not in provided:
                    errors.append(
                        f"{name} {package['version']}: active extra {extra!r} "
                        "is not declared via Provides-Extra"
                    )

    return {
        "lock": lock.get("case_id"),
        "status": "ok" if not errors else "failed",
        "errors": errors,
        "requirement_checks": checks,
        "closure_packages": sorted(packages),
        "active_extras": {
            name: sorted(extras) for name, extras in sorted(active_extras.items())
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independently audit lock files")
    parser.add_argument("locks", nargs="+")
    parser.add_argument("--wheelhouse", default="fixtures/wheelhouse")
    parser.add_argument("--out-dir", default="artifacts")
    args = parser.parse_args(argv)

    os.makedirs(args.out_dir, exist_ok=True)
    exit_code = 0
    for path in args.locks:
        with open(path, "r", encoding="utf-8") as fh:
            lock = json.load(fh)
        result = audit_lock(lock, args.wheelhouse)
        out = os.path.join(args.out_dir, f"{lock.get('case_id', 'lock')}.audit.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"{result['lock']}: {result['status']} -> {out}")
        if result["status"] != "ok":
            exit_code = 1
            for err in result["errors"]:
                print(f"  - {err}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
