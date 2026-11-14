"""Tests for the local wheel resolver.

Run with:  .venv/bin/python -m pytest tests -q
"""

from __future__ import annotations

import json
import os
import random
import shutil
import zipfile

import pytest

from depresolver.resolve import resolve_case
from depresolver.target import load_target_environment
from depresolver.wheels import load_wheelhouse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WHEELHOUSE = os.path.join(ROOT, "fixtures", "wheelhouse")
TARGET = os.path.join(ROOT, "fixtures", "target-env.json")


@pytest.fixture(scope="module")
def env():
    return load_target_environment(TARGET)


def _req_map(lock):
    return {p["name"]: p for p in lock["packages"]}


def test_late_extra_closure(env):
    index = load_wheelhouse(WHEELHOUSE)
    lock, report = resolve_case(
        {"case_id": "late_extra", "requirements": ["data-app==1.0"]}, index, env, []
    )
    assert report["status"] == "solvable"
    assert report["all_constraints_satisfied"] is True
    pkgs = _req_map(lock)
    # value-core is constrained by an early (audit-lib) and a late (view-lib)
    # edge; exactly one version must win and satisfy both.
    assert pkgs["value-core"]["version"] == "1.5"
    # view-lib is first pulled in without extras by data-app, then gains
    # [charts] from theme-addon; the extra must still activate plot-lite.
    assert pkgs["view-lib"]["active_extras"] == ["charts"]
    assert pkgs["view-lib"]["version"] == "1.0"
    assert "plot-lite" in pkgs
    # sys_platform == "linux" marker (target env) activates platform-util.
    assert "platform-util" in pkgs


def test_impossible_reports_chain(env):
    index = load_wheelhouse(WHEELHOUSE)
    lock, report = resolve_case(
        {
            "case_id": "impossible",
            "requirements": ["data-app==1.0", "value-core>=2"],
        },
        index,
        env,
        [],
    )
    assert report["status"] == "unsolvable"
    assert not lock
    assert report["conflicts"]
    text = json.dumps(report["conflicts"])
    # The conflict must name the interloping parents/requirements, not just fail.
    assert "value-core<2" in text
    assert "audit-lib 1.0" in text
    assert "root requires value-core>=2" in text


def test_charts_direct_prefers_feasible_solution(env):
    index = load_wheelhouse(WHEELHOUSE)
    lock, report = resolve_case(
        {"case_id": "charts_direct", "requirements": ["view-lib[charts]>=1,<3"]},
        index,
        env,
        [],
    )
    assert report["status"] == "solvable"
    pkgs = _req_map(lock)
    # view-lib 2.1 needs Python >=3.12 and must be filtered out for the 3.11
    # target; the feasible choice is 2.0 with its charts extra.
    assert pkgs["view-lib"]["version"] == "2.0"
    assert pkgs["view-lib"]["active_extras"] == ["charts"]
    assert pkgs["plot-lite"]["version"] == "2.0"


def test_requires_python_filtered(env):
    index = load_wheelhouse(WHEELHOUSE)
    versions = [c.version for c in index.versions("view-lib")]
    assert "2.1" in [str(v) for v in versions]
    cand = next(c for c in index.versions("view-lib") if str(c.version) == "2.1")
    assert cand.python_ok("3.11.0", "3.11") is False
    assert cand.python_ok("3.12.0", "3.12") is True


def test_determinism_under_shuffled_enumeration(env, monkeypatch):
    base = load_wheelhouse(WHEELHOUSE)
    base_lock, _ = resolve_case(
        {"case_id": "late_extra", "requirements": ["data-app==1.0"]}, base, env, []
    )
    files = sorted(os.listdir(WHEELHOUSE))
    canonical = json.dumps(base_lock, sort_keys=True)

    rng = random.Random(1234)
    for _ in range(5):
        shuffled = files[:]
        rng.shuffle(shuffled)
        real_listdir = os.listdir

        def fake_listdir(path, _shuffled=shuffled):
            if os.path.abspath(path) == os.path.abspath(WHEELHOUSE):
                return list(_shuffled)
            return real_listdir(path)

        monkeypatch.setattr(os, "listdir", fake_listdir)
        index = load_wheelhouse(WHEELHOUSE)
        lock, _ = resolve_case(
            {"case_id": "late_extra", "requirements": ["data-app==1.0"]},
            index,
            env,
            [],
        )
        monkeypatch.undo()
        assert json.dumps(lock, sort_keys=True) == canonical


def _make_wheel(directory, filename, metadata, wheel_tag="py3-none-any"):
    """Create a minimal wheel with the given METADATA text."""
    dist_info = filename.split("-py3-")[0] + ".dist-info"
    path = os.path.join(directory, filename)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{dist_info}/METADATA", metadata)
        zf.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\n"
            f"Tag: {wheel_tag}\n",
        )
        # Empty RECORD: no hashed entries to verify.
        zf.writestr(f"{dist_info}/RECORD", "")
    return path


def test_direct_url_rejected_before_solve(tmp_path):
    _make_wheel(
        str(tmp_path),
        "bad_dep-1.0-py3-none-any.whl",
        "Metadata-Version: 2.1\nName: bad-dep\nVersion: 1.0\n"
        "Requires-Dist: other @ https://example.invalid/other-1.0.whl\n",
    )
    index = load_wheelhouse(str(tmp_path))
    assert any(d.kind == "direct-url" for d in index.diagnostics)


def test_duplicate_metadata_header_rejected(tmp_path):
    _make_wheel(
        str(tmp_path),
        "dup_meta-1.0-py3-none-any.whl",
        "Metadata-Version: 2.1\nName: dup-meta\nVersion: 1.0\n"
        "Requires-Python: >=3.8\nRequires-Python: >=3.10\n",
    )
    index = load_wheelhouse(str(tmp_path))
    assert any(d.kind == "metadata-conflict" for d in index.diagnostics)


def test_identity_mismatch_rejected(tmp_path):
    _make_wheel(
        str(tmp_path),
        "wrong_name-1.0-py3-none-any.whl",
        "Metadata-Version: 2.1\nName: real-name\nVersion: 1.0\n",
    )
    index = load_wheelhouse(str(tmp_path))
    assert any(d.kind == "identity-mismatch" for d in index.diagnostics)


def test_non_pure_wheel_rejected(tmp_path):
    _make_wheel(
        str(tmp_path),
        "native_pkg-1.0-py3-none-any.whl",
        "Metadata-Version: 2.1\nName: native-pkg\nVersion: 1.0\n",
        wheel_tag="cp311-cp311-manylinux_2_17_x86_64",
    )
    index = load_wheelhouse(str(tmp_path))
    assert any(d.kind == "unsupported-format" for d in index.diagnostics)


def test_root_direct_url_is_input_error(env):
    index = load_wheelhouse(WHEELHOUSE)
    lock, report = resolve_case(
        {"case_id": "bad_root", "requirements": ["x @ https://e.invalid/x.whl"]},
        index,
        env,
        [],
    )
    assert report["status"] == "invalid-inputs"
    assert not lock
    assert any(e["kind"] == "direct-url" for e in report["input_errors"])


def test_global_consistency_no_split_versions(env):
    """A parent must never be allowed two versions at once."""
    index = load_wheelhouse(WHEELHOUSE)
    lock, report = resolve_case(
        {"case_id": "late_extra", "requirements": ["data-app==1.0"]}, index, env, []
    )
    names = [p["name"] for p in lock["packages"]]
    assert len(names) == len(set(names))


def test_marker_env_is_target_not_host(env, tmp_path):
    """A linux-only dependency is active for a linux target regardless of host."""
    _make_wheel(
        str(tmp_path),
        "host_marker-1.0-py3-none-any.whl",
        "Metadata-Version: 2.1\nName: host-marker\nVersion: 1.0\n"
        'Requires-Dist: platform-util==1.0; sys_platform == "linux"\n',
    )
    shutil.copy(
        os.path.join(WHEELHOUSE, "platform_util-1.0-py3-none-any.whl"), tmp_path
    )
    index = load_wheelhouse(str(tmp_path))
    lock, report = resolve_case(
        {"case_id": "m", "requirements": ["host-marker==1.0"]}, index, env, []
    )
    assert report["status"] == "solvable"
    assert "platform-util" in _req_map(lock)

    env_mac = dict(env, sys_platform="darwin", os_name="posix")
    lock2, report2 = resolve_case(
        {"case_id": "m", "requirements": ["host-marker==1.0"]}, index, env_mac, []
    )
    assert report2["status"] == "solvable"
    assert "platform-util" not in _req_map(lock2)


def test_backtracking_rejects_newer_parent(env, tmp_path):
    def mk(filename, metadata):
        _make_wheel(str(tmp_path), filename, metadata)

    mk(
        "app-1.0-py3-none-any.whl",
        "Metadata-Version: 2.1\nName: app\nVersion: 1.0\n"
        "Requires-Dist: parent>=1\nRequires-Dist: guard==1.0\n",
    )
    mk(
        "parent-2.0-py3-none-any.whl",
        "Metadata-Version: 2.1\nName: parent\nVersion: 2.0\n"
        "Requires-Dist: shared>=2\n",
    )
    mk(
        "parent-1.0-py3-none-any.whl",
        "Metadata-Version: 2.1\nName: parent\nVersion: 1.0\n"
        "Requires-Dist: shared>=1,<2\n",
    )
    mk(
        "guard-1.0-py3-none-any.whl",
        "Metadata-Version: 2.1\nName: guard\nVersion: 1.0\n"
        "Requires-Dist: shared<2\n",
    )
    mk(
        "shared-2.5-py3-none-any.whl",
        "Metadata-Version: 2.1\nName: shared\nVersion: 2.5\n",
    )
    mk(
        "shared-1.5-py3-none-any.whl",
        "Metadata-Version: 2.1\nName: shared\nVersion: 1.5\n",
    )
    index = load_wheelhouse(str(tmp_path))
    lock, report = resolve_case(
        {"case_id": "bt", "requirements": ["app==1.0"]}, index, env, []
    )
    assert report["status"] == "solvable"
    pkgs = _req_map(lock)
    # parent 2.0 (newest) is infeasible because guard pins shared<2.
    assert pkgs["parent"]["version"] == "1.0"
    assert pkgs["shared"]["version"] == "1.5"


def test_audit_accepts_real_lock(env):
    from depresolver.audit import audit_lock

    index = load_wheelhouse(WHEELHOUSE)
    lock, report = resolve_case(
        {"case_id": "late_extra", "requirements": ["data-app==1.0"]}, index, env, []
    )
    result = audit_lock(lock, WHEELHOUSE)
    assert result["status"] == "ok", result["errors"]


def test_audit_catches_tampered_version(env):
    from depresolver.audit import audit_lock

    index = load_wheelhouse(WHEELHOUSE)
    lock, _ = resolve_case(
        {"case_id": "late_extra", "requirements": ["data-app==1.0"]}, index, env, []
    )
    for package in lock["packages"]:
        if package["name"] == "value-core":
            package["version"] = "2.5"
            package["wheel"] = "value_core-2.5-py3-none-any.whl"
            package["wheel_path"] = os.path.join(WHEELHOUSE, "value_core-2.5-py3-none-any.whl")
    result = audit_lock(lock, WHEELHOUSE)
    assert result["status"] == "failed"
    assert any("value-core" in e for e in result["errors"])
