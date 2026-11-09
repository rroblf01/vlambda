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
import time
import uuid
import zipfile
from typing import Any

_PROJECTS_DIR = os.environ.get("VLAMBDA_PROJECTS_DIR", "/tmp/opencode/vlambda_projects")
_cache: dict[tuple, Any] = {}
_lock = threading.Lock()
_install_lock = threading.Lock()


def _safe_relative(path: str) -> bool:
    """Reject absolute paths, traversal and empty components."""
    if not isinstance(path, str) or path == "" or os.path.isabs(path) or "\\" in path:
        return False
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    return True


def _safe_extract(zf: zipfile.ZipFile, dest: str):
    os.makedirs(dest, exist_ok=True)
    max_mb = int(os.environ.get("VLAMBDA_PROJECT_MAX_MB", "50"))
    max_bytes = max(1, max_mb) * 1024 * 1024
    total = 0
    count = 0
    for info in zf.infolist():
        name = info.filename
        if name.endswith("/"):
            continue
        if os.path.isabs(name) or ".." in name.split("/") or "\\" in name:
            raise ValueError(f"unsafe path in archive: {name}")
        if name.startswith("__pycache__/") or "/__pycache__/" in name:
            continue
        count += 1
        total += max(0, info.file_size)
        if count > 2000 or total > max_bytes:
            raise ValueError("project archive is too large")
        target = os.path.join(dest, name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)


def _validated_dependency(line: str) -> str:
    """Accept PEP 508 requirements but never URLs, paths or pip options.

    The project upload runs outside the sandbox, so a direct reference or a
    local build could execute tenant code (or fetch an arbitrary URL) as the
    server.
    """
    value = line.strip()
    if not value or len(value) > 200 or value.startswith(("#", "-", ".", "/", "~")):
        raise RuntimeError(f"invalid package: {line}")
    if any(char in value for char in ("/", "\\", ":", "@")):
        raise RuntimeError(f"invalid package: {line}")
    if " -" in value:
        raise RuntimeError(f"invalid package: {line}")
    return value


def _collect_dependencies(project_dir: str) -> list[str]:
    deps: list[str] = []
    req = os.path.join(project_dir, "requirements.txt")
    if os.path.exists(req):
        with open(req, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    deps.append(_validated_dependency(line))
    pyproject = os.path.join(project_dir, "pyproject.toml")
    if os.path.exists(pyproject):
        try:
            import tomllib
            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
            project = data.get("project", {})
            for dep in project.get("dependencies", []) or []:
                deps.append(_validated_dependency(str(dep)))
            optional = project.get("optional-dependencies", {}) or {}
            for group in optional.values():
                for dep in group or []:
                    deps.append(_validated_dependency(str(dep)))
        except RuntimeError:
            raise
        except Exception as error:
            # Never fall back to installing the project itself: a build
            # backend would run tenant code on the server during upload.
            raise RuntimeError(f"invalid pyproject.toml: {error}") from error
    unique = list(dict.fromkeys(deps))
    if len(unique) > 100:
        raise RuntimeError("too many project dependencies (max 100)")
    return unique



_PACKAGES_DIR = os.environ.get("VLAMBDA_PACKAGES_DIR", "/tmp/opencode/vlambda_packages")
_PACKAGES_MAX_MB = int(os.environ.get("VLAMBDA_PACKAGES_MAX_MB", "1024"))


def _prune_packages():
    try:
        active = set()
        try:
            for name in os.listdir(_PROJECTS_DIR):
                vendor = os.path.join(_PROJECTS_DIR, name, "vendor")
                if os.path.islink(vendor):
                    active.add(os.path.realpath(vendor))
        except FileNotFoundError:
            pass
        total = 0
        entries = []
        for name in os.listdir(_PACKAGES_DIR):
            p = os.path.join(_PACKAGES_DIR, name)
            # Per-lambda targets and project-private vendors are not a cache:
            # deleting them would silently break a deployed lambda or project.
            if name == "lambdas" or not os.path.isdir(p):
                continue
            if os.path.islink(p):
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
            if os.path.realpath(p) in active:
                continue
            shutil.rmtree(p, ignore_errors=True)
            total -= size
    except Exception:
        pass


def _pip_install(target: str, deps: list[str]):
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "pip", "install", "--target", target, "--only-binary", ":all:", *deps]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--target", target,
               "--only-binary", ":all:", *deps]
    index_url = os.environ.get("VLAMBDA_PIP_INDEX_URL", "").strip()
    if index_url:
        cmd.extend(["--index-url", index_url])
    extra_index = os.environ.get("VLAMBDA_PIP_EXTRA_INDEX_URL", "").strip()
    if extra_index:
        cmd.extend(["--extra-index-url", extra_index])
    result = subprocess.run(cmd, timeout=300, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[-2000:])


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
    with _install_lock:
        os.makedirs(_PACKAGES_DIR, exist_ok=True)
        key = hashlib.sha256("\n".join(sorted(deps)).encode()).hexdigest()[:16]
        shared = os.path.join(_PACKAGES_DIR, key)
        if not os.path.exists(shared):
            # Install into a private directory first so a failed install never
            # leaves a partial tree that later uploads would reuse.
            tmp = shared + ".tmp-" + uuid.uuid4().hex
            os.makedirs(tmp, exist_ok=True)
            try:
                _pip_install(tmp, deps)
            except Exception:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            try:
                os.rename(tmp, shared)
            except OSError:
                shutil.rmtree(tmp, ignore_errors=True)
                if not os.path.isdir(shared):
                    raise
        # Copy rather than symlink: the sandbox allow-list only includes the
        # project directory, and a shared vendor symlink would point outside it.
        # A private copy also keeps one tenant's package edits away from others.
        shutil.copytree(shared, vendor)
        # Keep the project's dependency list next to the private vendor tree so
        # the packages API can list and extend it consistently.
        with open(os.path.join(vendor, "requirements.txt"), "w", encoding="utf-8") as handle:
            for dep in deps:
                handle.write(dep + "\n")
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
    if not _safe_relative(entrypoint):
        return json.dumps({"error": f"unsafe entrypoint: {entrypoint}"})

    slug = "p" + hashlib.sha256(data + str(owner_id).encode() + os.urandom(16)).hexdigest()[:15]
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
            else:
                raise RuntimeError(f"entrypoint not found: {entrypoint}")
        _install_dependencies(project_dir)
        import vlambda_native

        ok = vlambda_native.db_create_project(slug, name, method or "GET", entrypoint, project_dir, owner_id)
        if not ok:
            raise RuntimeError("failed to store project")
        return json.dumps({"slug": slug, "method": method or "GET", "entrypoint": entrypoint})
    except Exception as e:
        shutil.rmtree(project_dir, ignore_errors=True)
        return json.dumps({"error": str(e)})


def execute_project(project_dir: str, entrypoint: str, request) -> Any:
    from vlambda.async_support import resolve

    handler = _load_handler(project_dir, entrypoint)
    if handler is None:
        return {"error": "no handler function defined"}
    return resolve(handler(request))


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


def _project_files(project_dir: str) -> dict[str, str]:
    files: dict[str, str] = {}
    if not os.path.isdir(project_dir):
        return files
    for root, dirs, names in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in ("vendor", "__pycache__", ".git")]
        for name in names:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, project_dir)
            try:
                if os.path.getsize(full) > 2_000_000:
                    files[rel] = "<file too large>"
                    continue
                with open(full, "r", encoding="utf-8") as f:
                    files[rel] = f.read()
            except (UnicodeDecodeError, OSError):
                files[rel] = "<binary file>"
    return files


def get_project_json(project_dir: str, entrypoint: str, slug: str, method: str) -> str:
    return json.dumps({
        "slug": slug,
        "entrypoint": entrypoint,
        "method": method,
        "files": _project_files(project_dir),
    })


def save_project_json(project_dir: str, body_json: str, default_entrypoint: str = "main.py") -> str:
    try:
        data = json.loads(body_json)
    except Exception as e:
        return json.dumps({"error": f"invalid json: {e}"})
    files = data.get("files")
    if not isinstance(files, dict):
        return json.dumps({"error": "missing files"})
    for rel, content in files.items():
        if not _safe_relative(rel):
            return json.dumps({"error": f"unsafe path: {rel}"})
        full = os.path.join(project_dir, rel)
        root = os.path.realpath(project_dir)
        resolved = os.path.realpath(full)
        if resolved != root and not resolved.startswith(root + os.sep):
            return json.dumps({"error": f"unsafe path: {rel}"})
        os.makedirs(os.path.dirname(full) or project_dir, exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content if isinstance(content, str) else "")
    entrypoint = data.get("entrypoint") or default_entrypoint
    from vlambda.cache import clear as _clear_code_cache
    from vlambda.cache import invalidate_responses as _invalidate_responses
    _clear_code_cache()
    _invalidate_responses()
    with _lock:
        for key in [k for k in _cache if k[0] == project_dir]:
            del _cache[key]
    return json.dumps({"saved": True, "entrypoint": entrypoint})


def list_versions(slug: str) -> str:
    from vlambda.dbpool import get as db_get
    try:
        with db_get(os.environ.get("VLAMBDA_DB", "./vlambda.db")) as conn:
            rows = conn.execute(
                "SELECT version, created_at FROM lambda_versions WHERE slug = ? ORDER BY version DESC",
                (slug,),
            ).fetchall()
        return json.dumps({"versions": [{"version": r[0], "created_at": r[1]} for r in rows]})
    except Exception as e:
        return json.dumps({"versions": [], "error": str(e)})


def list_aliases(slug: str) -> str:
    from vlambda.dbpool import get as db_get
    prefix = f"{slug}:"
    try:
        with db_get(os.environ.get("VLAMBDA_DB", "./vlambda.db")) as conn:
            rows = conn.execute(
                "SELECT alias, version FROM lambda_aliases WHERE slug = ? ORDER BY alias",
                (slug,),
            ).fetchall()
        return json.dumps({"aliases": [
            {"alias": r[0][len(prefix):] if r[0].startswith(prefix) else r[0], "version": r[1]}
            for r in rows
        ]})
    except Exception as e:
        return json.dumps({"aliases": [], "error": str(e)})


def gc_orphans() -> int:
    """Delete project directories that no lambda row references anymore.

    Directories younger than the grace period are skipped: a project is
    extracted before its row is inserted, so an overlapping scan could
    otherwise delete a project that is still being uploaded.
    """
    from vlambda.dbpool import get as db_get
    db_path = os.environ.get("VLAMBDA_DB", "./vlambda.db")
    grace = int(os.environ.get("VLAMBDA_PROJECT_GC_GRACE", "300"))
    reference_dirs = set()
    referenced_slugs = set()
    try:
        with db_get(db_path) as conn:
            rows = conn.execute(
                "SELECT slug, kind, project_dir FROM lambdas"
            ).fetchall()
        for slug, kind, project_dir in rows:
            if slug:
                referenced_slugs.add(str(slug))
            if kind == 'project' and project_dir:
                reference_dirs.add(os.path.abspath(project_dir))
    except Exception:
        return 0
    referenced = reference_dirs
    removed = 0
    now = time.time()
    try:
        for name in os.listdir(_PROJECTS_DIR):
            path = os.path.abspath(os.path.join(_PROJECTS_DIR, name))
            if path in referenced:
                continue
            try:
                if now - os.path.getmtime(path) < grace:
                    continue
            except OSError:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    except FileNotFoundError:
        pass
    # Per-lambda package directories are independent of project directories;
    # remove them after the lambda row disappears.
    lambda_packages = os.path.join(_PACKAGES_DIR, "lambdas")
    try:
        for name in os.listdir(lambda_packages):
            if name in referenced_slugs:
                continue
            path = os.path.join(lambda_packages, name)
            try:
                if now - os.path.getmtime(path) < grace:
                    continue
            except OSError:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    except FileNotFoundError:
        pass
    return removed
