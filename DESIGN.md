# Local wheel dependency resolver

Offline resolver for the supplied `fixtures/wheelhouse`. It produces a
deterministic, globally consistent lock file and then verifies the lock in a
throwaway virtual environment. No network access, no source builds, and no pip
resolution are used to *choose* versions.

## Requirements

* Python 3.11
* `packaging` (declared in `requirements.txt`): `python -m pip install -r requirements.txt`

## Commands

```sh
# Resolve every case in fixtures/requests.json -> artifacts/<case>.lock.json
# and artifacts/<case>.report.json
python -m depresolver resolve

# Resolve a single case
python -m depresolver resolve --case late_extra

# Install each lock into an isolated temp venv using ONLY
# `pip install --no-index --no-deps <wheel>` and verify metadata + imports.
python -m depresolver verify artifacts/late_extra.lock.json artifacts/charts_direct.lock.json

# Independently re-derive the closure from the lock + wheelhouse and check
# every active constraint (does not trust solver internals).
python -m depresolver audit artifacts/late_extra.lock.json artifacts/charts_direct.lock.json
```

`resolve` reports both outcomes and exits 0 by default (the supplied fixture
set deliberately contains one unsolvable case). Pass `--strict` to exit
non-zero when any case is unsolvable or invalid. Unsolvable/invalid cases still
write a report with the conflicting requirement chains.

## What the resolver enforces

* **One version per normalized package name.** The assignment is global; a
  package is never satisfied twice with two versions.
* **Extras are additive until closure.** A package first pulled in normally can
  later receive an extra from another dependency; the extra's dependencies are
  activated and the closure iterates to a fixpoint. Root extras alone are never
  the whole story.
* **Markers use the target environment** (`fixtures/target-env.json`), not the
  host running the resolver. `extra == "..."` guards are evaluated per active
  extra.
* **`Requires-Python` filters candidates** against the target interpreter; an
  incompatible wheel cannot be selected.
* **Declaration semantics** for version specifiers and extras via `packaging`
  (`SpecifierSet`, `Requirement`, `Marker`, `Version`) — no string comparison.
* **Pre-solve diagnostics** for duplicate/conflicting metadata headers,
  identity mismatches (filename vs METADATA vs dist-info), RECORD hash/size
  mismatches, direct URL requirements, and non-`py3-none-any` wheels.
* **Deterministic choice.** Candidates are ordered newest-first per canonical
  name; directory enumeration order does not affect the selected solution.
* **Auditable failure.** An unsolvable case reports each constraint, its
  parent (including the parent's active extras) and the full root-to-leaf
  requirement chain.

## Layout

| File | Purpose |
| --- | --- |
| `depresolver/wheels.py` | wheel discovery, identity/RECORD validation, metadata parsing |
| `depresolver/target.py` | target environment loading and marker-variable validation |
| `depresolver/solver.py` | global backtracking solver, extras closure, conflict provenance |
| `depresolver/lock.py` | lock-file and report builders |
| `depresolver/resolve.py` | `resolve` CLI |
| `depresolver/verify.py` | `verify` CLI (isolated pip install + inspection) |
| `depresolver/audit.py` | independent closure/constraint auditor |
| `tests/test_resolver.py` | behavioural tests, incl. shuffle determinism |

## Lock file contents

Each package entry has: canonical `name`, exact `version`, `wheel` filename,
`sha256`, `active_extras`, `requires_python`, `top_level_modules`, the incoming
`reason_edges` (requested-by, raw requirement, specifier, requested extras,
marker, chain) and the `dependencies_emitted` by that wheel.

## Verification record

`verify` writes `<case>.install.json` with:

* pre-flight SHA256 checks for every wheel;
* the exact `pip install --no-index --no-deps` command and result per wheel;
* `importlib.metadata` version/name checks;
* real `import` of each top-level module, comparing `__version__`/`NAME`.

The temporary venv is deleted afterwards; the host/global environment is never
modified.
