"""Install a lock file into a throwaway venv and verify the result.

pip is used *only* to execute the already-decided installs:
``pip install --no-index --no-deps <wheel>``.  It performs no dependency
resolution of its own.  The current/global environment is never touched.

After installation we independently confirm, for every locked package:
  * the installed distribution's version matches the lock;
  * its metadata Name normalizes to the locked canonical name;
  * the top-level module imports and reports the expected ``__version__`` and
    ``NAME`` (when the synthetic modules expose them).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any


class VerifyError(RuntimeError):
    pass


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, check=False, **kwargs
    )


def _make_venv(base_dir: str) -> tuple[str, str]:
    """Create an isolated venv; return (python, pip)."""
    env_dir = os.path.join(base_dir, "venv")
    # ensurepip bundles pip/wheel with the interpreter, so this needs no network.
    result = _run([sys.executable, "-m", "venv", env_dir])
    if result.returncode != 0:
        raise VerifyError(f"venv creation failed: {result.stderr}")
    python = os.path.join(env_dir, "bin", "python")
    if not os.path.exists(python):
        python = os.path.join(env_dir, "Scripts", "python.exe")
    return python, f"{python} -m pip"


def _pip_install(pip_cmd: str, wheel: str, python: str) -> dict[str, Any]:
    cmd = (
        pip_cmd.split()
        + [
            "install",
            "--no-index",
            "--no-deps",
            "--disable-pip-version-check",
            "--no-cache-dir",
            wheel,
        ]
    )
    result = _run(cmd)
    return {
        "wheel": os.path.basename(wheel),
        "command": " ".join(cmd),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _inspect_installed(python: str, packages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run a probe inside the temp venv: metadata + real imports."""
    probes = [
        {
            "name": p["name"],
            "version": p["version"],
            "modules": p.get("top_level_modules", []),
        }
        for p in packages
    ]
    script = r"""
import json, re, sys
from importlib import import_module
from importlib.metadata import version, metadata, PackageNotFoundError

def canonicalize_name(name):
    # PEP 503 normalization, inlined so the probe needs no third-party packages.
    return re.sub(r"[-_.]+", "-", name).lower()

probes = json.loads(sys.argv[1])
out = []
for probe in probes:
    entry = {"name": probe["name"], "expected_version": probe["version"]}
    try:
        entry["metadata_version"] = version(probe["name"])
        md = metadata(probe["name"])
        entry["metadata_name"] = md["Name"]
        entry["normalized_metadata_name"] = canonicalize_name(md["Name"])
        entry["metadata_name_matches"] = canonicalize_name(md["Name"]) == probe["name"]
        entry["version_matches"] = entry["metadata_version"] == probe["version"]
    except PackageNotFoundError as exc:
        entry["error"] = f"distribution not found: {exc}"
    imports = []
    for module in probe["modules"]:
        item = {"module": module}
        try:
            mod = import_module(module)
            item["imported"] = True
            item["module_version"] = getattr(mod, "__version__", None)
            item["module_name"] = getattr(mod, "NAME", None)
            # Only assert a version match when the module actually exposes one.
            item["version_matches"] = (
                item["module_version"] == probe["version"]
                if item["module_version"] is not None
                else None
            )
        except Exception as exc:  # noqa: BLE001 - report, do not crash
            item["imported"] = False
            item["error"] = f"{type(exc).__name__}: {exc}"
        imports.append(item)
    entry["imports"] = imports
    out.append(entry)
print(json.dumps(out))
"""
    result = _run([python, "-c", script, json.dumps(probes)])
    if result.returncode != 0:
        raise VerifyError(f"inspection probe failed: {result.stderr}")
    return json.loads(result.stdout)


def verify_lock(
    lock_path: str, keep_env: bool = False, work_dir: str | None = None
) -> dict[str, Any]:
    with open(lock_path, "r", encoding="utf-8") as fh:
        lock = json.load(fh)
    packages = lock["packages"]

    # Pre-flight: every wheel exists and matches the locked hash.
    preflight: list[dict[str, Any]] = []
    ok = True
    for package in packages:
        wheel = package["wheel_path"]
        entry: dict[str, Any] = {
            "name": package["name"],
            "wheel": package["wheel"],
        }
        if not os.path.isfile(wheel):
            entry["error"] = "wheel missing"
            ok = False
        else:
            actual = sha256_file(wheel)
            entry["expected_sha256"] = package["sha256"]
            entry["actual_sha256"] = actual
            if actual != package["sha256"]:
                entry["error"] = "SHA256 mismatch"
                ok = False
        preflight.append(entry)
    if not ok:
        return {
            "lock": os.path.abspath(lock_path),
            "status": "failed",
            "stage": "preflight",
            "preflight": preflight,
        }

    base_dir = tempfile.mkdtemp(prefix="depverify-", dir=work_dir)
    record: dict[str, Any] = {
        "lock": os.path.abspath(lock_path),
        "case_id": lock.get("case_id"),
        "temporary_environment": base_dir,
        "cwd_environment_untouched": True,
        "preflight": preflight,
        "installations": [],
    }
    try:
        python, pip_cmd = _make_venv(base_dir)
        record["python"] = python
        record["pip"] = _run([python, "-m", "pip", "--version"]).stdout.strip()
        for package in packages:
            install = _pip_install(pip_cmd, package["wheel_path"], python)
            record["installations"].append(install)
            if install["returncode"] != 0:
                record["status"] = "failed"
                record["stage"] = "pip-install"
                return record

        inspection = _inspect_installed(python, packages)
        record["inspection"] = inspection
        all_ok = True
        for entry in inspection:
            if entry.get("error"):
                all_ok = False
                continue
            if not entry.get("version_matches") or not entry.get(
                "metadata_name_matches"
            ):
                all_ok = False
            for item in entry.get("imports", []):
                if not item.get("imported"):
                    all_ok = False
                elif item.get("version_matches") is False:
                    all_ok = False
        record["status"] = "verified" if all_ok else "failed"
        record["stage"] = "inspection" if not all_ok else "complete"
        # A minimal ``pip freeze``-style listing, produced by pip itself.
        record["pip_list"] = _run([python, "-m", "pip", "list", "--format=json"]).stdout
        return record
    finally:
        if not keep_env:
            shutil.rmtree(base_dir, ignore_errors=True)
            record["temporary_environment"] = "removed"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Install lock files into throwaway venvs and verify"
    )
    parser.add_argument("locks", nargs="+", help="lock JSON file(s)")
    parser.add_argument("--keep-env", action="store_true")
    parser.add_argument(
        "--record-dir", default="artifacts", help="where verification records go"
    )
    args = parser.parse_args(argv)

    os.makedirs(args.record_dir, exist_ok=True)
    exit_code = 0
    for lock_path in args.locks:
        record = verify_lock(lock_path, keep_env=args.keep_env)
        case_id = record.get("case_id") or os.path.basename(lock_path)
        out = os.path.join(args.record_dir, f"{case_id}.install.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)
            fh.write("\n")
        status = record.get("status")
        print(f"{case_id}: {status} -> {out}")
        if status != "verified":
            exit_code = 1
            for install in record.get("installations", []):
                if install["returncode"] != 0:
                    print(install["stderr"], file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
