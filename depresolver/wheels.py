"""Wheel discovery, identity checks and metadata parsing.

A :class:`Candidate` is only created for a wheel that passed every structural
check.  Anything we cannot faithfully represent (direct URLs, non-pure tags,
malformed or duplicated metadata) is recorded as a :class:`Diagnostic` before
the solver runs, so a bad wheel can never silently poison a solution.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import zipfile
from dataclasses import dataclass, field
from typing import Iterable

from packaging.markers import Marker
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

SUPPORTED_TAG = "py3-none-any"


@dataclass(frozen=True)
class Diagnostic:
    """A problem found before (or during) solving.

    ``kind`` is one of ``direct-url``, ``unsupported-format``,
    ``metadata-conflict``, ``identity-mismatch``, ``hash-mismatch``,
    ``malformed-metadata``, ``invalid-requirement``, ``missing-file``.
    """

    kind: str
    subject: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "subject": self.subject, "detail": self.detail}

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"[{self.kind}] {self.subject}: {self.detail}"


@dataclass(frozen=True)
class Dependency:
    """A declared dependency edge of one wheel.

    ``marker`` is the raw marker string (``None`` when unconditional);
    ``extras`` are the extras requested on the *child* requirement.
    """

    name: str  # canonical name
    specifier: SpecifierSet
    extras: frozenset[str]
    marker: str | None
    raw: str

    def applies_with(
        self, extras: frozenset[str], environment: dict[str, str]
    ) -> bool:
        """Whether this edge is active when the parent has ``extras`` active."""
        return evaluate_marker(self.marker, extras, environment)


def evaluate_marker(
    marker: str | None,
    active_extras: Iterable[str],
    environment: dict[str, str],
) -> bool:
    """Evaluate a marker against the *target* environment for all active extras.

    The supplied environment provides every PEP 508 variable except ``extra``,
    which is set per extra here.  A dependency guarded by ``extra == "x"`` is
    active as soon as ``x`` is an activated extra of the parent; an
    unconditional edge is always active.
    """
    if marker is None:
        return True
    parsed = Marker(marker)
    extras = set(active_extras)
    if not extras:
        return parsed.evaluate({**environment, "extra": ""})
    for extra in sorted(extras):
        if parsed.evaluate({**environment, "extra": extra}):
            return True
    return False


@dataclass
class Candidate:
    """One installable wheel that passed all identity checks."""

    name: str  # canonical
    version: Version
    path: str
    filename: str
    sha256: str
    requires_python: SpecifierSet | None
    dependencies: tuple[Dependency, ...]
    provides_extras: frozenset[str]
    metadata_text: str
    top_level: tuple[str, ...] = ()

    @property
    def requires_python_display(self) -> str:
        return str(self.requires_python) if self.requires_python else "<none>"

    def python_ok(self, python_full_version: str, python_version: str) -> bool:
        if not self.requires_python:
            return True
        # PEP 503 installers evaluate Requires-Python against the full
        # interpreter version; ``python_version`` is only a fallback.
        version = python_full_version or python_version
        return self.requires_python.contains(
            Version(version), prereleases=True
        )

    def active_dependencies(
        self, extras: frozenset[str], environment: dict[str, str]
    ) -> list[Dependency]:
        return [
            dep for dep in self.dependencies if dep.applies_with(extras, environment)
        ]


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_record(zf: zipfile.ZipFile, dist_info: str) -> Diagnostic | None:
    """Verify every hashed RECORD entry.  Returns a diagnostic on mismatch."""
    record_name = f"{dist_info}/RECORD"
    try:
        raw = zf.read(record_name).decode("utf-8")
    except KeyError:
        return Diagnostic("metadata-conflict", dist_info, "missing RECORD")
    for row in csv.reader(io.StringIO(raw)):
        if not row or not row[0]:
            continue
        filename = row[0]
        if len(row) < 2 or not row[1]:
            continue  # RECORD itself, or an intentionally unhashed entry
        algo, _, expected_b64 = row[1].partition("=")
        if not algo or not expected_b64:
            return Diagnostic(
                "metadata-conflict", filename, f"malformed RECORD hash {row[1]!r}"
            )
        try:
            hasher = hashlib.new(algo)
        except ValueError:
            return Diagnostic(
                "metadata-conflict", filename, f"unknown RECORD hash algorithm {algo!r}"
            )
        try:
            content = zf.read(filename)
        except KeyError:
            return Diagnostic(
                "metadata-conflict", filename, "RECORD lists a missing file"
            )
        hasher.update(content)
        actual_b64 = base64.urlsafe_b64encode(hasher.digest()).rstrip(b"=").decode()
        if actual_b64 != expected_b64.strip():
            return Diagnostic(
                "hash-mismatch", filename, "RECORD hash does not match contents"
            )
        if len(row) >= 3 and row[2]:
            try:
                declared_size = int(row[2])
            except ValueError:
                return Diagnostic(
                    "metadata-conflict", filename, f"malformed RECORD size {row[2]!r}"
                )
            if declared_size != len(content):
                return Diagnostic(
                    "hash-mismatch", filename, "RECORD size does not match contents"
                )
    return None


def _parse_metadata(
    text: str, subject: str
) -> tuple[
    str | None,
    Version | None,
    SpecifierSet | None,
    list[Dependency] | None,
    frozenset[str],
    Diagnostic | None,
]:
    """Parse METADATA using ``email`` semantics (one field per logical key)."""
    from email.parser import HeaderParser

    parser = HeaderParser()
    msg = parser.parsestr(text)

    def one(field_name: str) -> tuple[str | None, Diagnostic | None]:
        values = msg.get_all(field_name) or []
        if len(values) > 1:
            return None, Diagnostic(
                "metadata-conflict",
                subject,
                f"duplicate {field_name} header: {values!r}",
            )
        return (values[0] if values else None), None

    raw_name, err = one("Name")
    if err:
        return None, None, None, None, frozenset(), err
    raw_version, err = one("Version")
    if err:
        return None, None, None, None, frozenset(), err
    if not raw_name or not raw_version:
        return (
            None,
            None,
            None,
            None,
            frozenset(),
            Diagnostic("malformed-metadata", subject, "Name or Version missing"),
        )

    name = canonicalize_name(raw_name)
    try:
        version = Version(raw_version)
    except InvalidVersion:
        return (
            None,
            None,
            None,
            None,
            frozenset(),
            Diagnostic(
                "malformed-metadata", subject, f"invalid version {raw_version!r}"
            ),
        )

    rp_values = msg.get_all("Requires-Python") or []
    if len(rp_values) > 1:
        return (
            None,
            None,
            None,
            None,
            frozenset(),
            Diagnostic(
                "metadata-conflict", subject, "duplicate Requires-Python header"
            ),
        )
    requires_python: SpecifierSet | None = None
    if rp_values:
        try:
            requires_python = SpecifierSet(rp_values[0])
        except InvalidSpecifier:
            return (
                None,
                None,
                None,
                None,
                frozenset(),
                Diagnostic(
                    "malformed-metadata",
                    subject,
                    f"invalid Requires-Python {rp_values[0]!r}",
                ),
            )

    extras = {
        canonicalize_name(e.strip())
        for e in (msg.get_all("Provides-Extra") or [])
        if e.strip()
    }

    dependencies: list[Dependency] = []
    for raw in msg.get_all("Requires-Dist") or []:
        try:
            req = Requirement(raw)
        except InvalidRequirement as exc:
            return (
                None,
                None,
                None,
                None,
                frozenset(extras),
                Diagnostic("invalid-requirement", subject, f"{raw!r}: {exc}"),
            )
        if req.url is not None:
            return (
                None,
                None,
                None,
                None,
                frozenset(extras),
                Diagnostic(
                    "direct-url",
                    subject,
                    f"direct URL requirement is unsupported: {raw!r}",
                ),
            )
        dependencies.append(
            Dependency(
                name=canonicalize_name(req.name),
                specifier=req.specifier,
                extras=frozenset(canonicalize_name(e) for e in req.extras),
                marker=str(req.marker) if req.marker is not None else None,
                raw=raw,
            )
        )

    return name, version, requires_python, dependencies, frozenset(extras), None


def load_candidate(path: str) -> tuple[Candidate | None, Diagnostic | None]:
    """Validate and parse a single wheel file."""
    filename = os.path.basename(path)
    if not filename.endswith(".whl"):
        # Ignore non-wheel artefacts such as sdists / zips in the directory.
        return None, None
    try:
        _, _, _, tags = parse_wheel_filename(filename)
    except (InvalidWheelFilename, InvalidVersion) as exc:
        return None, Diagnostic(
            "unsupported-format", filename, f"unparseable wheel filename: {exc}"
        )
    tag_names = {str(tag) for tag in tags}
    if tag_names != {SUPPORTED_TAG}:
        return None, Diagnostic(
            "unsupported-format",
            filename,
            f"only {SUPPORTED_TAG} wheels are supported, got {sorted(tag_names)}",
        )

    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            dist_infos = sorted(
                {
                    n.split("/", 1)[0]
                    for n in names
                    if n.split("/", 1)[0].endswith(".dist-info")
                }
            )
            if len(dist_infos) != 1:
                return None, Diagnostic(
                    "metadata-conflict",
                    filename,
                    f"expected exactly one .dist-info directory, found {dist_infos}",
                )
            dist_info = dist_infos[0]
            metadata_members = [
                n for n in names if n == f"{dist_info}/METADATA"
            ]
            if len(metadata_members) != 1:
                return None, Diagnostic(
                    "metadata-conflict",
                    filename,
                    "expected exactly one METADATA member",
                )
            wheel_members = [n for n in names if n == f"{dist_info}/WHEEL"]
            if len(wheel_members) != 1:
                return None, Diagnostic(
                    "metadata-conflict", filename, "expected exactly one WHEEL member"
                )

            wheel_text = zf.read(wheel_members[0]).decode("utf-8", "replace")
            wheel_tags = [
                line.split(":", 1)[1].strip()
                for line in wheel_text.splitlines()
                if line.lower().startswith("tag:")
            ]
            if set(wheel_tags) != {SUPPORTED_TAG}:
                return None, Diagnostic(
                    "unsupported-format",
                    filename,
                    f"WHEEL Tag(s) {wheel_tags} are not {SUPPORTED_TAG}",
                )

            record_diag = _verify_record(zf, dist_info)
            if record_diag:
                return None, record_diag

            metadata_text = zf.read(metadata_members[0]).decode("utf-8", "replace")
            top_level = tuple(
                sorted(
                    {
                        (
                            n.split("/", 1)[0]
                            if "/" in n
                            else n[: -len(".py")]
                        )
                        for n in names
                        if n.endswith(".py")
                        and not n.split("/", 1)[0].endswith(".dist-info")
                    }
                )
            )
    except (zipfile.BadZipFile, OSError) as exc:
        return None, Diagnostic(
            "malformed-metadata", filename, f"cannot read wheel: {exc}"
        )

    name, version, requires_python, deps, extras, diag = _parse_metadata(
        metadata_text, filename
    )
    if diag:
        return None, diag

    assert name is not None and version is not None and deps is not None

    try:
        file_name, file_version, _, _ = parse_wheel_filename(filename)
    except (InvalidWheelFilename, InvalidVersion) as exc:  # pragma: no cover
        return None, Diagnostic(
            "identity-mismatch", filename, f"cannot parse identity: {exc}"
        )
    if file_name != name:
        return None, Diagnostic(
            "identity-mismatch",
            filename,
            f"filename name {file_name!r} != METADATA Name {name!r}",
        )
    if file_version != version:
        return None, Diagnostic(
            "identity-mismatch",
            filename,
            f"filename version {file_version} != METADATA Version {version}",
        )
    stem = dist_info[: -len(".dist-info")]
    dist_name_part, _, dist_version_part = stem.rpartition("-")
    try:
        dist_version = Version(dist_version_part)
    except InvalidVersion:
        dist_version = None
    if (
        canonicalize_name(dist_name_part.replace("_", "-")) != name
        or dist_version is None
        or dist_version != version
    ):
        return None, Diagnostic(
            "identity-mismatch",
            filename,
            f"dist-info directory {stem!r} disagrees with METADATA",
        )

    candidate = Candidate(
        name=name,
        version=version,
        path=os.path.abspath(path),
        filename=filename,
        sha256=sha256_file(path),
        requires_python=requires_python,
        dependencies=tuple(deps),
        provides_extras=extras,
        metadata_text=metadata_text,
        top_level=top_level,
    )
    return candidate, None


@dataclass
class WheelIndex:
    """All valid candidates keyed by canonical name."""

    candidates: dict[str, list[Candidate]] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def add(self, candidate: Candidate) -> None:
        self.candidates.setdefault(candidate.name, []).append(candidate)

    def versions(self, name: str) -> list[Candidate]:
        # Sort newest-first for a deterministic preference order, independent of
        # directory enumeration order.  The solver may still backtrack.
        return sorted(
            self.candidates.get(name, []),
            key=lambda c: (c.version, c.filename),
            reverse=True,
        )

    def names(self) -> list[str]:
        return sorted(self.candidates)


def load_wheelhouse(directory: str) -> WheelIndex:
    """Read a directory of wheels.  Deduplicates identical files by content."""
    index = WheelIndex()
    if not os.path.isdir(directory):
        index.diagnostics.append(
            Diagnostic("missing-file", directory, "wheelhouse directory not found")
        )
        return index

    seen_sha: dict[tuple[str, Version], str] = {}
    for filename in sorted(os.listdir(directory)):
        path = os.path.join(directory, filename)
        if not os.path.isfile(path) or not filename.endswith(".whl"):
            continue
        candidate, diag = load_candidate(path)
        if diag:
            index.diagnostics.append(diag)
            continue
        if candidate is None:
            continue
        key = (candidate.name, candidate.version)
        if key in seen_sha:
            # Two files claim the same normalized name/version.
            if seen_sha[key] != candidate.sha256:
                index.diagnostics.append(
                    Diagnostic(
                        "metadata-conflict",
                        candidate.filename,
                        "duplicate name/version with different contents "
                        f"({candidate.filename} vs {os.path.basename(seen_sha[key])})",
                    )
                )
                continue
            # Byte-identical duplicate: keep one copy.
            continue
        seen_sha[key] = candidate.path
        index.add(candidate)
    return index
