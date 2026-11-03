"""Export, import and duplicate lambdas and projects as portable JSON."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import time

EXPORT_FORMAT = "vlambda/v1"


def _db_path() -> str:
    return os.environ.get("VLAMBDA_DB", "./vlambda.db")


def _connect():
    from vlambda.dbpool import get as db_get

    return db_get(_db_path())


def _read_project_files(project_dir: str) -> tuple[dict[str, str], list[str]]:
    """Return text files in a project directory plus the list of skipped binaries."""
    files: dict[str, str] = {}
    skipped: list[str] = []
    if not os.path.isdir(project_dir):
        return files, skipped
    for root, dirs, names in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in ("vendor", "__pycache__", ".git")]
        for name in names:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, project_dir).replace(os.sep, "/")
            try:
                if os.path.getsize(full) > 2_000_000:
                    skipped.append(rel)
                    continue
                with open(full, "r", encoding="utf-8") as handle:
                    files[rel] = handle.read()
            except (UnicodeDecodeError, OSError):
                skipped.append(rel)
    return files, skipped


def export_lambda(slug: str, owner_id: str) -> str:
    """Serialize a lambda (code or project) owned by owner_id."""
    try:
        user_id = int(owner_id)
    except (TypeError, ValueError):
        return ""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT slug, name, code, method, kind, entrypoint, project_dir, timeout, memory_mb, version"
                " FROM lambdas WHERE slug = ? AND owner_id = ?",
                (slug, user_id),
            ).fetchone()
    except Exception as e:
        return json.dumps({"error": f"export failed: {e}"})
    if row is None:
        return ""

    kind = row[4] or "code"
    payload: dict = {
        "format": EXPORT_FORMAT,
        "slug": row[0],
        "name": row[1] or "",
        "method": row[3] or "GET",
        "kind": kind,
        "timeout": row[7] or 0,
        "memory_mb": row[8] or 0,
        "version": row[9] or 1,
    }
    if kind == "project":
        payload["entrypoint"] = row[5] or "main.py"
        files, skipped = _read_project_files(row[6] or "")
        payload["files"] = files
        if skipped:
            payload["skipped_files"] = skipped
    else:
        payload["code"] = row[2] or ""
    return json.dumps(payload)


def _new_slug(prefix: str, seed: str) -> str:
    unique = seed + ":" + str(time.time_ns()) + ":" + os.urandom(8).hex()
    return prefix + hashlib.sha256(unique.encode()).hexdigest()[:15]


def _normalize_files(files) -> tuple[dict[str, str], str]:
    if not isinstance(files, dict):
        return {}, "missing files"
    result: dict[str, str] = {}
    for rel, content in files.items():
        if not isinstance(rel, str) or os.path.isabs(rel) or ".." in rel.split("/") or "\\" in rel:
            return {}, f"unsafe path: {rel}"
        if not isinstance(content, str):
            continue
        result[rel.replace(os.sep, "/")] = content
    return result, ""


def import_lambda(body_json: str, owner_id: str) -> str:
    """Create a lambda or project from an export payload."""
    import vlambda_native

    try:
        user_id = int(owner_id)
    except (TypeError, ValueError):
        return json.dumps({"error": "invalid owner"})
    try:
        max_lambdas = int(os.environ.get("VLAMBDA_MAX_LAMBDAS", "20"))
    except ValueError:
        max_lambdas = 20
    if vlambda_native.db_count_lambdas(user_id) >= max_lambdas:
        return json.dumps({"error": "lambda quota exceeded"})
    try:
        data = json.loads(body_json)
    except Exception as e:
        return json.dumps({"error": f"invalid json: {e}"})
    if not isinstance(data, dict):
        return json.dumps({"error": "body must be a JSON object"})

    kind = data.get("kind") or ("project" if "files" in data else "code")
    name = (data.get("name") or "imported").strip() or "imported"
    method = (data.get("method") or "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        method = "GET"

    if kind == "project":
        files, err = _normalize_files(data.get("files"))
        if err:
            return json.dumps({"error": err})
        if not files:
            return json.dumps({"error": "project has no files"})
        entrypoint = (data.get("entrypoint") or "main.py").lstrip("/")
        if entrypoint not in files:
            for candidate in ("main.py", "handler.py", "app.py"):
                if candidate in files:
                    entrypoint = candidate
                    break
            else:
                return json.dumps({"error": f"entrypoint not found: {entrypoint}"})

        from vlambda.project import _PROJECTS_DIR

        slug = _new_slug("p", json.dumps(files, sort_keys=True))
        project_dir = os.path.join(_PROJECTS_DIR, slug)
        os.makedirs(project_dir, exist_ok=True)
        try:
            for rel, content in files.items():
                full = os.path.join(project_dir, rel)
                os.makedirs(os.path.dirname(full) or project_dir, exist_ok=True)
                with open(full, "w", encoding="utf-8") as handle:
                    handle.write(content)
            ok = vlambda_native.db_create_project(slug, name, method, entrypoint, project_dir, user_id)
            if not ok:
                raise RuntimeError("failed to store project")
        except Exception as e:
            import shutil

            shutil.rmtree(project_dir, ignore_errors=True)
            return json.dumps({"error": f"import failed: {e}"})
        return json.dumps({"slug": slug, "kind": "project", "entrypoint": entrypoint})

    code = data.get("code")
    if not isinstance(code, str) or not code.strip():
        return json.dumps({"error": "missing code"})
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return json.dumps({"error": f"syntax error: {e}"})
    if not any(isinstance(node, ast.FunctionDef) and node.name == "handler" for node in ast.walk(tree)):
        return json.dumps({"error": "missing handler function"})

    slug = _new_slug("l", code)
    try:
        timeout = int(data.get("timeout") or 0)
        memory_mb = int(data.get("memory_mb") or 0)
    except (TypeError, ValueError):
        timeout, memory_mb = 0, 0
    ok = vlambda_native.db_create_lambda(slug, name, code, method, user_id, timeout, memory_mb)
    if not ok:
        return json.dumps({"error": "failed to store lambda"})
    return json.dumps({"slug": slug, "kind": "code", "method": method})


def duplicate_lambda(slug: str, owner_id: str) -> str:
    """Copy a lambda owned by owner_id with a new name and slug."""
    exported = export_lambda(slug, owner_id)
    if not exported:
        return ""
    try:
        data = json.loads(exported)
    except Exception as e:
        return json.dumps({"error": f"duplicate failed: {e}"})
    if "error" in data:
        return exported
    data["name"] = (data.get("name") or slug) + " (copy)"
    data.pop("slug", None)
    data.pop("version", None)
    return import_lambda(json.dumps(data), owner_id)
