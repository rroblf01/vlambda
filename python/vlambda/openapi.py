"""OpenAPI 3.1 generation for vlambda lambdas."""

from __future__ import annotations

import json
import os

from vlambda.dbpool import get as db_get

_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}


def _db_path() -> str:
    return os.environ.get("VLAMBDA_DB", "./vlambda.db")


def _version() -> str:
    from vlambda.version import __version__

    return __version__


def _owned(slug: str, owner_id: int) -> bool:
    with db_get(_db_path()) as conn:
        row = conn.execute(
            "SELECT 1 FROM lambdas WHERE slug = ? AND owner_id = ?", (slug, int(owner_id))
        ).fetchone()
    return row is not None


def get_meta(slug: str, owner_id: str) -> str:
    try:
        owner = int(owner_id)
        with db_get(_db_path()) as conn:
            row = conn.execute(
                "SELECT summary, description, tags FROM lambdas WHERE slug = ? AND owner_id = ?",
                (slug, owner),
            ).fetchone()
        if row is None:
            return json.dumps({"error": "not found"})
        return json.dumps({
            "summary": row[0] or "",
            "description": row[1] or "",
            "tags": row[2] or "",
        })
    except Exception as error:
        return json.dumps({"error": f"{type(error).__name__}: {error}"})


def _text_field(data: dict, key: str, limit: int) -> str:
    value = data.get(key)
    if value is None:
        return ""
    if isinstance(value, (dict, tuple, set)) or value is True or value is False:
        raise ValueError(f"{key} must be a string")
    return str(value)[:limit]


def set_meta(slug: str, owner_id: str, body: str) -> str:
    try:
        owner = int(owner_id)
        data = json.loads(body or "{}")
        if not isinstance(data, dict):
            return json.dumps({"error": "body must be an object"})
        summary = _text_field(data, "summary", 200)
        description = _text_field(data, "description", 4000)
        raw_tags = data.get("tags")
        if isinstance(raw_tags, list):
            if not all(isinstance(tag, str) for tag in raw_tags):
                return json.dumps({"error": "tags must be a string or a list of strings"})
            raw_tags = ",".join(raw_tags)
        elif raw_tags is not None and not isinstance(raw_tags, str):
            return json.dumps({"error": "tags must be a string or a list of strings"})
        tags = ",".join(
            part.strip()[:40]
            for part in str(raw_tags or "").split(",")
            if part.strip()
        )[:500]
        with db_get(_db_path()) as conn:
            owned = conn.execute(
                "SELECT 1 FROM lambdas WHERE slug = ? AND owner_id = ?", (slug, owner)
            ).fetchone()
            if owned is None:
                return json.dumps({"error": "not found"})
            conn.execute(
                "UPDATE lambdas SET summary = ?, description = ?, tags = ?"
                " WHERE slug = ? AND owner_id = ?",
                (summary, description, tags, slug, owner),
            )
            conn.commit()
        return json.dumps({"summary": summary, "description": description, "tags": tags})
    except Exception as error:
        return json.dumps({"error": f"{type(error).__name__}: {error}"})


def _operation(row: dict) -> dict:
    method = row["method"].lower()
    operation = {
        "operationId": f"invoke_{row['slug']}",
        "summary": row["summary"] or row["name"] or row["slug"],
        "description": row["description"] or (
            "Invoke this vlambda handler. The response body is produced by the handler."
        ),
        "tags": [tag for tag in (row["tags"] or "").split(",") if tag] or ["lambdas"],
        "x-vlambda-slug": row["slug"],
        "responses": {
            "200": {
                "description": "Handler response",
                "headers": {
                    "X-Request-Id": {
                        "description": "Invocation identifier",
                        "schema": {"type": "string"},
                    }
                },
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/AnyValue"}
                    },
                    "text/plain": {"schema": {"type": "string"}},
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"}
                    },
                },
            },
            "500": {
                "description": "Handler error",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"error": {"type": "string"}},
                        }
                    }
                },
            },
        },
    }
    if method in ("post", "put", "patch", "delete"):
        operation["requestBody"] = {
            "required": False,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/AnyValue"}},
                "text/plain": {"schema": {"type": "string"}},
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
            },
        }
    return operation


def build(owner_id: int = 0, base_url: str = "") -> dict:
    owner = int(owner_id or 0)
    with db_get(_db_path()) as conn:
        if owner:
            rows = conn.execute(
                "SELECT slug, name, method, kind, summary, description, tags"
                " FROM lambdas WHERE owner_id = ? ORDER BY slug",
                (owner,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT slug, name, method, kind, summary, description, tags"
                " FROM lambdas ORDER BY slug"
            ).fetchall()
    paths = {}
    for raw in rows:
        row = {
            "slug": raw[0], "name": raw[1], "method": raw[2].upper(), "kind": raw[3],
            "summary": raw[4] or "", "description": raw[5] or "", "tags": raw[6] or "",
        }
        if row["method"] not in _METHODS:
            continue
        path = f"/f/{row['slug']}"
        paths.setdefault(path, {})[row["method"].lower()] = _operation(row)
        if row["kind"] == "project":
            paths.setdefault(path + "/{path}", {})["get"] = {
                "operationId": f"static_{row['slug']}",
                "summary": f"Static files for {row['name'] or row['slug']}",
                "tags": ["static"],
                "parameters": [{
                    "name": "path",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                }],
                "responses": {
                    "200": {"description": "Static file"},
                    "404": {"description": "File not found"},
                },
            }
    servers = []
    if base_url:
        servers.append({"url": base_url.rstrip("/")})
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "vlambda",
            "version": _version(),
            "description": (
                "HTTP endpoints for vlambda lambdas. Public invocation endpoints are "
                f"served under {base_url or '/f'}; management is handled by the vlambda API."
            ),
        },
        "servers": servers,
        "paths": paths,
        "components": {
            "schemas": {
                "AnyValue": {
                    "description": "Any JSON value produced or accepted by a handler.",
                }
            },
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                }
            },
        },
    }


def handle_api(owner_id: str, base_url: str = "") -> str:
    try:
        return json.dumps(build(int(owner_id or 0), base_url))
    except Exception as error:
        return json.dumps({"error": f"{type(error).__name__}: {error}"})
