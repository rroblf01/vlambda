"""Install PyPI packages into per-lambda or per-project vendor directories."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading

_LOCK = threading.Lock()
_PACKAGES_DIR = os.environ.get("VLAMBDA_PACKAGES_DIR", "/tmp/opencode/vlambda_packages")
_MAX_PACKAGES = int(os.environ.get("VLAMBDA_MAX_PACKAGES", "30"))
_SPEC = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9_,.-]+\])?([<>=!~][^;]*)?$")


def _validate(package: str) -> str:
    package = package.strip()
    if not package or package.startswith("-") or "://" in package or package.startswith("git+"):
        raise ValueError(f"invalid package: {package}")
    if "/" in package or "\\" in package or package.startswith("."):
        raise ValueError(f"invalid package: {package}")
    if not _SPEC.match(package):
        raise ValueError(f"invalid package spec: {package}")
    return package


def _requirements_path(target_dir: str) -> str:
    return os.path.join(target_dir, "requirements.txt")


def read_requirements(target_dir: str) -> list[str]:
    path = _requirements_path(target_dir)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]


def _write_requirements(target_dir: str, packages: list[str]):
    os.makedirs(target_dir, exist_ok=True)
    with open(_requirements_path(target_dir), "w", encoding="utf-8") as f:
        for package in packages:
            f.write(package + "\n")


def _run_install(target_dir: str):
    req = _requirements_path(target_dir)
    uv = shutil.which("uv")
    if uv:
        cmd = [
            uv, "pip", "install",
            "--python", sys.executable,
            "--target", target_dir,
            "--only-binary", ":all:",
            "-r", req,
        ]
    else:
        cmd = [
            sys.executable, "-m", "pip", "install",
            "--target", target_dir,
            "--only-binary", ":all:",
            "-r", req,
        ]
    index_url = os.environ.get("VLAMBDA_PIP_INDEX_URL", "").strip()
    if index_url:
        cmd.extend(["--index-url", index_url])
    extra_index = os.environ.get("VLAMBDA_PIP_EXTRA_INDEX_URL", "").strip()
    if extra_index:
        cmd.extend(["--extra-index-url", extra_index])
    if os.environ.get("VLAMBDA_PIP_REQUIRE_HASHES", "0").lower() in ("1", "true", "yes", "on"):
        cmd.append("--require-hashes")
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("VLAMBDA_PACKAGES_TIMEOUT", "180")),
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[-2000:])


def install(target_dir: str, packages: list[str]) -> str:
    if not packages:
        return json.dumps({"error": "no packages provided"})
    with _LOCK:
        try:
            existing = read_requirements(target_dir)
            merged = list(dict.fromkeys(existing + [_validate(p) for p in packages]))
            if len(merged) > _MAX_PACKAGES:
                return json.dumps({"error": f"too many packages (max {_MAX_PACKAGES})"})
            _write_requirements(target_dir, merged)
            _run_install(target_dir)
            return json.dumps({"installed": merged, "target": target_dir})
        except Exception as e:
            return json.dumps({"error": str(e)})


def list_installed(target_dir: str) -> str:
    packages = read_requirements(target_dir)
    return json.dumps({"packages": packages, "target": target_dir})


def lambda_target(slug: str) -> str:
    return os.path.join(_PACKAGES_DIR, "lambdas", slug)


def project_target(project_dir: str) -> str:
    return os.path.join(project_dir, "vendor")


def list_for(slug: str, kind: str, project_dir: str = "") -> str:
    target = project_target(project_dir) if kind == "project" and project_dir else lambda_target(slug)
    return list_installed(target)


def install_for(slug: str, kind: str, project_dir: str, packages_json: str) -> str:
    target = project_target(project_dir) if kind == "project" and project_dir else lambda_target(slug)
    try:
        data = json.loads(packages_json)
    except Exception as e:
        return json.dumps({"error": f"invalid json: {e}"})
    packages = data.get("packages") if isinstance(data, dict) else data
    if isinstance(packages, str):
        packages = [p for p in packages.replace(",", "\n").splitlines() if p.strip()]
    if not isinstance(packages, list):
        return json.dumps({"error": "packages must be a list or string"})
    result = install(target, [str(p) for p in packages])
    # Invalidate the project handler cache after installing dependencies.
    if kind == "project" and project_dir:
        try:
            from vlambda import project
            with project._lock:
                for key in [k for k in project._cache if k[0] == project_dir]:
                    del project._cache[key]
        except Exception:
            pass
    return result
