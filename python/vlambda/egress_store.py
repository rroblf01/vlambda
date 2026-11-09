"""Per-lambda egress allow-lists."""

from __future__ import annotations

import json
import re

from vlambda.dbpool import get as db_get

_MAX_HOSTS = 20
_HOST_RE = re.compile(
    r"^(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)


class EgressError(ValueError):
    pass


def _db_path() -> str:
    import os

    return os.environ.get("VLAMBDA_DB", "./vlambda.db")


def _normalize(pattern: str) -> str:
    if not isinstance(pattern, str):
        raise EgressError(f"invalid host pattern: {pattern!r}")
    value = pattern.strip().lower().rstrip(".")
    if not value or len(value) > 253 or not _HOST_RE.match(value):
        raise EgressError(f"invalid host pattern: {pattern!r}")
    return value


def list_hosts(slug: str) -> list[str]:
    with db_get(_db_path()) as conn:
        rows = conn.execute(
            "SELECT pattern FROM lambda_egress WHERE slug = ? ORDER BY pattern",
            (slug,),
        ).fetchall()
    return [row[0] for row in rows]


def set_hosts(slug: str, hosts) -> list[str]:
    if not isinstance(hosts, list):
        raise EgressError("hosts must be a list")
    normalized = []
    for host in hosts:
        value = _normalize(host)
        if value not in normalized:
            normalized.append(value)
    if len(normalized) > _MAX_HOSTS:
        raise EgressError(f"too many egress hosts (max {_MAX_HOSTS})")
    with db_get(_db_path()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM lambda_egress WHERE slug = ?", (slug,))
            for pattern in normalized:
                conn.execute(
                    "INSERT INTO lambda_egress (slug, pattern) VALUES (?, ?)",
                    (slug, pattern),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return normalized


def host_allowed(slug: str, host: str) -> bool:
    host = str(host or "").strip().lower().rstrip(".")
    if not host:
        return False
    for pattern in list_hosts(slug):
        if pattern.startswith("*."):
            suffix = pattern[2:]
            if host == suffix or host.endswith("." + suffix):
                return True
        elif host == pattern:
            return True
    return False


def handle_api(slug: str, owner_id: str, method: str, body: str) -> str:
    try:
        owner = int(owner_id)
    except (TypeError, ValueError):
        return json.dumps({"error": "invalid owner"})
    try:
        with db_get(_db_path()) as conn:
            row = conn.execute(
                "SELECT 1 FROM lambdas WHERE slug = ? AND owner_id = ?",
                (slug, owner),
            ).fetchone()
            if row is None:
                return json.dumps({"error": "not found"})
        if method == "GET":
            return json.dumps({"hosts": list_hosts(slug)})
        if method == "PUT":
            try:
                payload = json.loads(body or "{}")
            except Exception as error:
                return json.dumps({"error": f"invalid json: {error}"})
            if isinstance(payload, dict):
                hosts = payload.get("hosts")
            else:
                hosts = payload
            return json.dumps({"hosts": set_hosts(slug, hosts)})
        return json.dumps({"error": "method not allowed"})
    except EgressError as error:
        return json.dumps({"error": str(error)})
    except Exception as error:
        return json.dumps({"error": f"{type(error).__name__}: {error}"})
