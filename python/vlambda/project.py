"""Support for multi-file Python projects uploaded as ZIP archives."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
import zipfile
from typing import Any

_PROJECTS_DIR = os.environ.get("VLAMBDA_PROJECTS_DIR", "/tmp/opencode/vlambda_projects")
_cache: dict[tuple, Any] = {}
_lock = threading.Lock()


def _safe_extract(zf: zipfile.ZipFile, dest: str):
    os.makedirs(dest, exist_ok=True)
    for info in zf.infolist():
        name = info.filename
        if name.endswith("/"):
            continue
        if os.path.isabs(name) or ".." in name.split("/"):
            raise ValueError(f"unsafe path in archive: {name}")
        if name.startswith("__pycache__/") or "/__pycache__/" in name:
            continue
        target = os.path.join(dest, name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)


def _collect_dependencies(project_dir: str) -> list[str]:
    deps: list[str] = []
    req = os.path.join(project_dir, "requirements.txt")
    if os.path.exists(req):
        with open(req, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    deps.append(line)
    pyproject = os.path.join(project_dir, "pyproject.toml")
    if os.path.exists(pyproject):
        try:
            import tomllib
            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
            project = data.get("project", {})
            deps.extend(project.get("dependencies", []) or [])
            optional = project.get("optional-dependencies", {}) or {}
            for group in optional.values():
                deps.extend(group or [])
        except Exception:
            # Fall back to installing the project itself if parsing fails.
            deps.append(".")
    return deps


_PACKAGES_DIR = os.environ.get("VLAMBDA_PACKAGES_DIR", "/tmp/opencode/vlambda_packages")
_PACKAGES_MAX_MB = int(os.environ.get("VLAMBDA_PACKAGES_MAX_MB", "1024"))


def _prune_packages():
    try:
        total = 0
        entries = []
        for name in os.listdir(_PACKAGES_DIR):
            p = os.path.join(_PACKAGES_DIR, name)
            if not os.path.isdir(p):
                continue
            size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(p) for f in fs)
            total += size
            entries.append((os.path.getmtime(p), p, size))
        limit = _PACKAGES_MAX_MB * 1024 * 1024
        if total <= limit:
            return
        entries.sort()
        for _, p, size in entries:
            if total <= limit:
                break
            shutil.rmtree(p, ignore_errors=True)
            total -= size
    except Exception:
        pass


def _install_dependencies(project_dir: str):
    deps = _collect_dependencies(project_dir)
    vendor = os.path.join(project_dir, "vendor")
    if os.path.lexists(vendor):
        if os.path.isdir(vendor) and not os.path.islink(vendor):
            shutil.rmtree(vendor, ignore_errors=True)
        else:
            os.remove(vendor)
    if not deps:
        os.makedirs(vendor, exist_ok=True)
        return
    os.makedirs(_PACKAGES_DIR, exist_ok=True)
    key = hashlib.sha256("\n".join(sorted(deps)).encode()).hexdigest()[:16]
    shared = os.path.join(_PACKAGES_DIR, key)
    if not os.path.exists(shared):
        os.makedirs(shared, exist_ok=True)
        uv = shutil.which("uv")
        if uv:
            cmd = [uv, "pip", "install", "--target", shared, *deps]
        else:
            cmd = [sys.executable, "-m", "pip", "install", "--target", shared, *deps]
        subprocess.run(cmd, check=True, timeout=300, capture_output=True, text=True)
    try:
        os.symlink(shared, vendor)
    except OSError:
        shutil.copytree(shared, vendor)
    _prune_packages()


def _module_name(project_dir: str, entrypoint: str) -> str:
    h = hashlib.sha256((project_dir + ":" + entrypoint).encode()).hexdigest()[:12]
    return f"vlambda_project_{h}"


def _load_handler(project_dir: str, entrypoint: str):
    key = (project_dir, entrypoint)
    with _lock:
        cached = _cache.get(key)
        if cached is not None:
            return cached
    full = os.path.join(project_dir, entrypoint)
    if not os.path.exists(full):
        raise FileNotFoundError(f"entrypoint not found: {entrypoint}")
    for path in (project_dir, os.path.join(project_dir, "vendor")):
        if path not in sys.path:
            sys.path.insert(0, path)
    name = _module_name(project_dir, entrypoint)
    spec = importlib.util.spec_from_file_location(name, full)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {entrypoint}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    handler = getattr(mod, "handler", None)
    with _lock:
        _cache[key] = handler
    return handler


def create_project(zip_b64: str, entrypoint: str, name: str, method: str, owner_id: int) -> str:
    import json

    try:
        data = base64.b64decode(zip_b64, validate=True)
    except Exception as e:
        return json.dumps({"error": f"invalid base64: {e}"})

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        return json.dumps({"error": f"invalid zip: {e}"})

    if not entrypoint:
        entrypoint = "main.py"
    entrypoint = entrypoint.lstrip("/")

    slug = "p" + hashlib.sha256(data).hexdigest()[:15]
    project_dir = os.path.join(_PROJECTS_DIR, slug)
    if os.path.exists(project_dir):
        shutil.rmtree(project_dir)
    os.makedirs(_PROJECTS_DIR, exist_ok=True)

    try:
        _safe_extract(zf, project_dir)
        zf.close()
        if not os.path.exists(os.path.join(project_dir, entrypoint)):
            # Fall back to common entrypoints.
            for candidate in ("main.py", "handler.py", "app.py"):
                if os.path.exists(os.path.join(project_dir, candidate)):
                    entrypoint = candidate
                    break
        _install_dependencies(project_dir)
        import vlambda_native

        ok = vlambda_native.db_create_project(slug, name, method or "GET", entrypoint, project_dir, owner_id)
        if not ok:
            raise RuntimeError("failed to store project")
        # Warm the import cache.
        _load_handler(project_dir, entrypoint)
        return json.dumps({"slug": slug, "method": method or "GET", "entrypoint": entrypoint})
    except Exception as e:
        shutil.rmtree(project_dir, ignore_errors=True)
        return json.dumps({"error": str(e)})


def execute_project(project_dir: str, entrypoint: str, request) -> Any:
    handler = _load_handler(project_dir, entrypoint)
    if handler is None:
        return {"error": "no handler function defined"}
    return handler(request)


def delete_project(project_dir: str):
    with _lock:
        for key in [k for k in _cache if k[0] == project_dir]:
            del _cache[key]
    shutil.rmtree(project_dir, ignore_errors=True)


def schedule_delete(project_dir: str):
    """Delete a project directory without blocking the request thread."""
    def _run():
        try:
            delete_project(project_dir)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()
    return None
