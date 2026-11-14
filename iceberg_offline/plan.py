"""Loading and enforcing plan.json (fixed URI -> local path mapping)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import ResolutionError, UnsupportedError

_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class UriMapping:
    prefix: str
    root: Path


@dataclass(frozen=True)
class Plan:
    path: Path
    base_dir: Path
    metadata_ref: str
    mappings: tuple[UriMapping, ...]
    snapshots: tuple[int, ...]
    supported: str

    @classmethod
    def load(cls, plan_path: str | Path) -> "Plan":
        path = Path(plan_path).expanduser().resolve()
        if not path.is_file():
            raise ResolutionError(f"plan file does not exist: {path}")
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raise ResolutionError("plan.json must contain a JSON object")

        base_dir = path.parent.resolve()
        metadata_ref = raw.get("metadata")
        if not isinstance(metadata_ref, str) or not metadata_ref:
            raise ResolutionError("plan.json is missing a non-empty 'metadata' string")

        uri_mapping = raw.get("uri_mapping")
        if not isinstance(uri_mapping, dict) or not uri_mapping:
            raise ResolutionError("plan.json is missing a non-empty 'uri_mapping' object")

        mappings: list[UriMapping] = []
        seen_prefixes: set[str] = set()
        for prefix, target in uri_mapping.items():
            if not isinstance(prefix, str) or not _URI_RE.match(prefix):
                raise ResolutionError(f"uri_mapping prefix is not a URI: {prefix!r}")
            if prefix in seen_prefixes:
                raise ResolutionError(f"duplicate uri_mapping prefix: {prefix!r}")
            seen_prefixes.add(prefix)
            if not isinstance(target, str) or not target:
                raise ResolutionError(f"uri_mapping target for {prefix!r} must be a string")
            root = (base_dir / target).resolve()
            # A mapping root itself must stay below the plan directory.  The
            # real fixtures map ``table/`` -> fixtures/table, but this guard
            # also rejects ``../../etc`` style plans.
            if not _is_relative_to(root, base_dir):
                raise ResolutionError(
                    f"uri_mapping target for {prefix!r} escapes the plan directory: {target!r}"
                )
            mappings.append(UriMapping(prefix, root))

        # Longest prefix first so a more specific mapping wins.
        mappings.sort(key=lambda m: len(m.prefix), reverse=True)

        snapshot_ids_raw = raw.get("snapshots", [])
        if not isinstance(snapshot_ids_raw, list):
            raise ResolutionError("plan.json 'snapshots' must be a list")
        snapshot_ids: list[int] = []
        for value in snapshot_ids_raw:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ResolutionError(f"snapshot id must be an integer: {value!r}")
            snapshot_ids.append(value)

        supported = raw.get("supported", "")
        if not isinstance(supported, str):
            raise ResolutionError("plan.json 'supported' must be a string")

        return cls(
            path=path,
            base_dir=base_dir,
            metadata_ref=metadata_ref,
            mappings=tuple(mappings),
            snapshots=tuple(snapshot_ids),
            supported=supported,
        )

    def resolve(self, ref: str) -> Path:
        """Resolve a manifest/metadata URI or a plan-relative local path.

        * URIs must match one of the fixed mapped prefixes.
        * Relative paths are interpreted relative to plan.json.
        * Absolute local paths are rejected (the plan is the only allowed root).
        * The result must stay inside the selected root, following symlinks.
        """
        if not isinstance(ref, str) or not ref:
            raise ResolutionError("empty path/URI")
        if _URI_RE.match(ref):
            for mapping in self.mappings:
                if not ref.startswith(mapping.prefix):
                    continue
                suffix = ref[len(mapping.prefix) :]
                candidate = (mapping.root / suffix).resolve()
                if not _is_relative_to(candidate, mapping.root):
                    raise ResolutionError(
                        f"resolved path escapes mapped root {mapping.root}: {ref!r}"
                    )
                return candidate
            raise ResolutionError(
                f"URI is not covered by plan.json uri_mapping: {ref!r}"
            )

        # Reject platform-specific absolute paths outright; fixtures use only
        # plan-relative metadata paths and mapped s3 URIs.
        p = Path(ref)
        if p.is_absolute():
            raise ResolutionError(f"absolute local path is not allowed: {ref!r}")
        candidate = (self.base_dir / p).resolve()
        if not _is_relative_to(candidate, self.base_dir):
            raise ResolutionError(
                f"local path escapes the plan directory: {ref!r}"
            )
        return candidate

    def local_uri_for(self, ref: str) -> str | None:
        """Return the mapped local path as a display string, if resolvable."""
        try:
            return str(self.resolve(ref))
        except ResolutionError:
            return None

    def validate_snapshot_selection(self, snapshot_ids: Iterable[int]) -> None:
        if not self.snapshots:
            return
        allowed = set(self.snapshots)
        missing = [sid for sid in snapshot_ids if sid not in allowed]
        if missing:
            raise UnsupportedError(
                "unsupported: snapshot id(s) not listed in plan.json: "
                + ", ".join(str(x) for x in missing)
            )
