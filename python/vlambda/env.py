"""Per-lambda environment variables and encrypted secrets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from typing import Any

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_MAX_VARS = 50
_MAX_BYTES = 16 * 1024
_cache: dict[str, tuple[float, dict[str, str]]] = {}
_CACHE_TTL = 2.0


def _db_path() -> str:
    return os.environ.get("VLAMBDA_DB", "./vlambda.db")


def _master_key() -> bytes:
    raw = os.environ.get("VLAMBDA_ENCRYPTION_KEY") or os.environ.get("VLAMBDA_SECRET", "")
    if not raw:
        raise RuntimeError("VLAMBDA_SECRET or VLAMBDA_ENCRYPTION_KEY is required for secrets")
    return hashlib.sha256(("vlambda-env:" + raw).encode()).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(out[:length])


def encrypt(value: str) -> str:
    key = _master_key()
    nonce = secrets.token_bytes(16)
    data = value.encode("utf-8")
    cipher = bytes(a ^ b for a, b in zip(data, _keystream(key, nonce, len(data))))
    tag = hmac.new(key, b"vlambda-env:" + nonce + cipher, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + tag + cipher).decode("ascii")


def decrypt(token: str) -> str:
    key = _master_key()
    raw = base64.urlsafe_b64decode(token.encode("ascii"))
    nonce, tag, cipher = raw[:16], raw[16:48], raw[48:]
    expected = hmac.new(key, b"vlambda-env:" + nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("invalid secret payload")
    data = bytes(a ^ b for a, b in zip(cipher, _keystream(key, nonce, len(cipher))))
    return data.decode("utf-8")


def _connect():
    from vlambda.dbpool import get as db_get
    return db_get(_db_path())


def _owned(slug: str, owner_id: int) -> bool:
    try:
        with _connect() as conn:
            row = conn.execute("SELECT 1 FROM lambdas WHERE slug = ? AND owner_id = ?", (slug, int(owner_id))).fetchone()
        return row is not None
    except Exception:
        return False


def _public(row) -> dict:
    key, is_secret, value = row
    return {
        "key": key,
        "secret": bool(is_secret),
        "value": "********" if is_secret and value else (value if not is_secret else ""),
    }


def list_env(slug: str, owner_id: str = "0") -> str:
    try:
        with _connect() as conn:
            owned = conn.execute("SELECT 1 FROM lambdas WHERE slug = ? AND owner_id = ?", (slug, int(owner_id))).fetchone()
            if owned is None:
                return json.dumps({"error": "not found"})
            rows = conn.execute("SELECT key, is_secret, value FROM lambda_env WHERE slug = ? ORDER BY key", (slug,)).fetchall()
        return json.dumps({"env": [_public(row) for row in rows]})
    except Exception as e:
        return json.dumps({"error": str(e)})


def get_env(slug: str) -> dict[str, str]:
    now = time.time()
    cached = _cache.get(slug)
    if cached and cached[0] > now:
        return dict(cached[1])
    values: dict[str, str] = {}
    try:
        with _connect() as conn:
            rows = conn.execute("SELECT key, is_secret, value FROM lambda_env WHERE slug = ?", (slug,)).fetchall()
        for key, is_secret, value in rows:
            values[key] = decrypt(value) if is_secret else value
    except Exception:
        values = {}
    _cache[slug] = (now + _CACHE_TTL, values)
    return dict(values)


def clear_cache(slug: str | None = None):
    if slug is None:
        _cache.clear()
    else:
        _cache.pop(slug, None)


def save_env(slug: str, owner_id: str, body: str) -> str:
    try:
        data = json.loads(body or "{}")
    except Exception as e:
        return json.dumps({"error": f"invalid json: {e}"})
    if not isinstance(data, dict):
        return json.dumps({"error": "body must be an object"})
    plain = data.get("vars") or data.get("env") or {}
    secret = data.get("secrets") or {}
    if not isinstance(plain, dict) or not isinstance(secret, dict):
        return json.dumps({"error": "vars and secrets must be objects"})
    merged: dict[str, tuple[bool, str]] = {}
    try:
        with _connect() as conn:
            owned = conn.execute("SELECT 1 FROM lambdas WHERE slug = ? AND owner_id = ?", (slug, int(owner_id))).fetchone()
            if owned is None:
                return json.dumps({"error": "not found"})
            existing = dict(conn.execute("SELECT key, value FROM lambda_env WHERE slug = ? AND is_secret = 1", (slug,)).fetchall())
            for key, value in plain.items():
                if not _KEY_RE.match(str(key)):
                    raise ValueError(f"invalid environment name: {key}")
                merged[str(key)] = (False, str(value))
            for key, value in secret.items():
                key = str(key)
                if not _KEY_RE.match(key):
                    raise ValueError(f"invalid environment name: {key}")
                if value == "********" and key in existing:
                    continue
                merged[key] = (True, encrypt(str(value)))
            if len(merged) > _MAX_VARS:
                raise ValueError(f"too many environment variables (max {_MAX_VARS})")
            if sum(len(k) + len(v) for k, (_, v) in merged.items()) > _MAX_BYTES:
                raise ValueError(f"environment too large (max {_MAX_BYTES} bytes)")
            conn.execute("DELETE FROM lambda_env WHERE slug = ?", (slug,))
            for key, (is_secret, value) in merged.items():
                conn.execute("INSERT INTO lambda_env (slug, key, is_secret, value) VALUES (?, ?, ?, ?)",
                             (slug, key, 1 if is_secret else 0, value))
            conn.commit()
    except Exception as e:
        return json.dumps({"error": str(e)})
    clear_cache(slug)
    return list_env(slug, owner_id)
