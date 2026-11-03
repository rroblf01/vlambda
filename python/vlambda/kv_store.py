"""Per-lambda key/value storage and HTTP API helpers.

Values are stored as typed JSON envelopes so strings, numbers, booleans,
nulls, JSON documents and bytes round-trip without surprises.
"""

from __future__ import annotations

import base64
import json
import math
import re
import time

from vlambda.dbpool import get as db_get

_KEY_RE = re.compile(r"^[A-Za-z0-9_.:@+-]{1,128}$")
_MAX_KEYS = 256
_MAX_VALUE_BYTES = 64 * 1024
_MAX_TOTAL_BYTES = 1024 * 1024
_PREFIX_RE = re.compile(r"^[A-Za-z0-9_.:@+-]{0,128}$")


class KVError(ValueError):
    """Invalid KV operation or quota exceeded."""


def _db_path() -> str:
    import os

    return os.environ.get("VLAMBDA_DB", "./vlambda.db")


def _now() -> float:
    return time.time()


def _validate_key(key: str) -> str:
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise KVError("invalid key")
    return key


def _validate_prefix(prefix: str) -> str:
    if not isinstance(prefix, str) or not _PREFIX_RE.match(prefix):
        raise KVError("invalid prefix")
    return prefix


def _encode(value) -> str:
    if value is None:
        envelope = {"t": "null", "v": None}
    elif isinstance(value, bool):
        envelope = {"t": "bool", "v": value}
    elif isinstance(value, int):
        envelope = {"t": "int", "v": value}
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise KVError("non-finite numbers are not supported")
        envelope = {"t": "float", "v": value}
    elif isinstance(value, str):
        envelope = {"t": "str", "v": value}
    elif isinstance(value, (bytes, bytearray)):
        envelope = {"t": "bytes", "v": base64.b64encode(bytes(value)).decode("ascii")}
    else:
        envelope = {
            "t": "json",
            "v": json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
        }
    encoded = json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_VALUE_BYTES:
        raise KVError(f"value exceeds {_MAX_VALUE_BYTES} bytes")
    return encoded


def _http_value(value):
    if isinstance(value, (bytes, bytearray)):
        return {
            "binary": True,
            "value_b64": base64.b64encode(bytes(value)).decode("ascii"),
        }
    return {"binary": False, "value": value}


def _decode(encoded: str):
    data = json.loads(encoded)
    kind = data.get("t")
    value = data.get("v")
    if kind == "null":
        return None
    if kind == "bytes":
        return base64.b64decode(value.encode("ascii"))
    if kind == "json":
        return json.loads(value)
    return value


def _owned(conn, slug: str, owner_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM lambdas WHERE slug = ? AND owner_id = ?", (slug, int(owner_id))
    ).fetchone()
    return row is not None


def _purge_expired(conn, slug: str, now: float) -> None:
    conn.execute(
        "DELETE FROM lambda_kv WHERE slug = ? AND expires_at IS NOT NULL AND expires_at <= ?",
        (slug, now),
    )


def get_value(slug: str, key: str):
    """Return ``(exists, value, expires_at)`` for a runtime key."""
    key = _validate_key(key)
    with db_get(_db_path()) as conn:
        row = conn.execute(
            "SELECT value, expires_at FROM lambda_kv WHERE slug = ? AND key = ?",
            (slug, key),
        ).fetchone()
        if row is None:
            return False, None, None
        if row[1] is not None and row[1] <= _now():
            conn.execute("DELETE FROM lambda_kv WHERE slug = ? AND key = ?", (slug, key))
            conn.commit()
            return False, None, None
        return True, _decode(row[0]), row[1]


def set_value(slug: str, key: str, value, ttl: float | None = None):
    key = _validate_key(key)
    if ttl is not None:
        if not isinstance(ttl, (int, float)) or isinstance(ttl, bool):
            raise KVError("ttl must be a positive number of seconds")
        ttl = float(ttl)
        if not math.isfinite(ttl) or ttl <= 0:
            raise KVError("ttl must be a positive number of seconds")
    encoded = _encode(value)
    now = _now()
    expires = now + ttl if ttl is not None else None
    with db_get(_db_path()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _purge_expired(conn, slug, now)
            previous = conn.execute(
                "SELECT value FROM lambda_kv WHERE slug = ? AND key = ?", (slug, key)
            ).fetchone()
            if previous is None:
                count = conn.execute(
                    "SELECT COUNT(*) FROM lambda_kv WHERE slug = ?", (slug,)
                ).fetchone()[0]
                if count >= _MAX_KEYS:
                    raise KVError(f"key quota exceeded (max {_MAX_KEYS})")
            total = conn.execute(
                "SELECT COALESCE(SUM(LENGTH(key) + LENGTH(value)), 0) FROM lambda_kv WHERE slug = ?",
                (slug,),
            ).fetchone()[0]
            if previous is not None:
                total -= len(key) + len(previous[0])
            total += len(key) + len(encoded)
            if total > _MAX_TOTAL_BYTES:
                raise KVError(f"storage quota exceeded (max {_MAX_TOTAL_BYTES} bytes)")
            conn.execute(
                "INSERT INTO lambda_kv (slug, key, value, expires_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(slug, key) DO UPDATE SET"
                " value = excluded.value, expires_at = excluded.expires_at,"
                " updated_at = excluded.updated_at",
                (slug, key, encoded, expires, now),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"key": key, "expires_at": expires}


def delete_value(slug: str, key: str) -> bool:
    key = _validate_key(key)
    with db_get(_db_path()) as conn:
        cur = conn.execute("DELETE FROM lambda_kv WHERE slug = ? AND key = ?", (slug, key))
        conn.commit()
        return cur.rowcount > 0


def clear_values(slug: str) -> int:
    with db_get(_db_path()) as conn:
        cur = conn.execute("DELETE FROM lambda_kv WHERE slug = ?", (slug,))
        conn.commit()
        return max(0, cur.rowcount)


def list_values(slug: str, prefix: str = "") -> list[dict]:
    prefix = _validate_prefix(prefix)
    now = _now()
    with db_get(_db_path()) as conn:
        _purge_expired(conn, slug, now)
        conn.commit()
        rows = conn.execute(
            "SELECT key, value, expires_at FROM lambda_kv WHERE slug = ? ORDER BY key",
            (slug,),
        ).fetchall()
    items = []
    for key, encoded, expires in rows:
        if prefix and not key.startswith(prefix):
            continue
        items.append({"key": key, "value": _decode(encoded), "expires_at": expires})
    return items


def incr_value(slug: str, key: str, amount: int | float = 1):
    key = _validate_key(key)
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        raise KVError("amount must be a number")
    if not math.isfinite(float(amount)):
        raise KVError("amount must be finite")
    now = _now()
    with db_get(_db_path()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT value, expires_at FROM lambda_kv WHERE slug = ? AND key = ?",
                (slug, key),
            ).fetchone()
            if row is not None and row[1] is not None and row[1] <= now:
                conn.execute("DELETE FROM lambda_kv WHERE slug = ? AND key = ?", (slug, key))
                row = None
            if row is None:
                count = conn.execute(
                    "SELECT COUNT(*) FROM lambda_kv WHERE slug = ?", (slug,)
                ).fetchone()[0]
                if count >= _MAX_KEYS:
                    raise KVError(f"key quota exceeded (max {_MAX_KEYS})")
            current = _decode(row[0]) if row is not None else 0
            if isinstance(current, bool) or not isinstance(current, (int, float)):
                raise KVError("value is not a number")
            new_value = current + amount
            encoded = _encode(new_value)
            if len(encoded.encode("utf-8")) > _MAX_VALUE_BYTES:
                raise KVError(f"value exceeds {_MAX_VALUE_BYTES} bytes")
            previous_len = len(row[0]) if row is not None else 0
            total = conn.execute(
                "SELECT COALESCE(SUM(LENGTH(key) + LENGTH(value)), 0) FROM lambda_kv WHERE slug = ?",
                (slug,),
            ).fetchone()[0]
            total -= previous_len
            total += len(encoded)
            if total > _MAX_TOTAL_BYTES:
                raise KVError(f"storage quota exceeded (max {_MAX_TOTAL_BYTES} bytes)")
            conn.execute(
                "INSERT INTO lambda_kv (slug, key, value, expires_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(slug, key) DO UPDATE SET"
                " value = excluded.value, expires_at = excluded.expires_at,"
                " updated_at = excluded.updated_at",
                (slug, key, encoded, row[1] if row is not None else None, now),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"key": key, "value": new_value}


def handle_api(slug: str, owner_id: str, method: str, key: str, body: str, query: str = "") -> str:
    """HTTP callback used by the V routes."""
    from urllib.parse import parse_qs
    try:
        owner = int(owner_id)
    except (TypeError, ValueError):
        return json.dumps({"error": "invalid owner"})
    try:
        with db_get(_db_path()) as conn:
            if not _owned(conn, slug, owner):
                return json.dumps({"error": "not found"})
        if method == "GET":
            if key == "":
                prefix = parse_qs(query or "").get("prefix", [""])[0]
                items = []
                for entry in list_values(slug, prefix):
                    item = {"key": entry["key"], "expires_at": entry.get("expires_at")}
                    item.update(_http_value(entry.get("value")))
                    items.append(item)
                return json.dumps({"items": items})
            exists, value, expires = get_value(slug, key)
            item = {"exists": exists, "expires_at": expires}
            item.update(_http_value(value))
            return json.dumps(item)
        if method == "PUT":
            try:
                payload = json.loads(body or "{}")
            except Exception as error:
                return json.dumps({"error": f"invalid json: {error}"})
            if not isinstance(payload, dict):
                return json.dumps({"error": "body must be an object"})
            if "value_b64" in payload:
                try:
                    value = base64.b64decode(str(payload["value_b64"]).encode("ascii"))
                except Exception:
                    return json.dumps({"error": "invalid value_b64"})
            else:
                value = payload.get("value")
            ttl = payload.get("ttl")
            result = set_value(slug, key, value, ttl)
            return json.dumps(result)
        if method == "DELETE":
            if key == "":
                return json.dumps({"deleted": clear_values(slug)})
            return json.dumps({"deleted": delete_value(slug, key)})
        return json.dumps({"error": "method not allowed"})
    except KVError as error:
        return json.dumps({"error": str(error)})
    except Exception as error:
        return json.dumps({"error": f"{type(error).__name__}: {error}"})
