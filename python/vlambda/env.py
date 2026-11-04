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

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_RESERVED_PREFIXES = ("VLAMBDA_", "VLRUN_", "LD_")
_RESERVED_NAMES = {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT"}
_MAX_VARS = 50
_MAX_BYTES = 16 * 1024
_cache: dict[str, tuple[float, dict[str, str]]] = {}
_CACHE_TTL = 2.0


def _valid_key(key: str) -> bool:
    return bool(_KEY_RE.match(key)) and key not in _RESERVED_NAMES and not key.startswith(_RESERVED_PREFIXES)


def _db_path() -> str:
    return os.environ.get("VLAMBDA_DB", "./vlambda.db")


def _key_from(raw: str) -> bytes:
    if not raw:
        raise RuntimeError("VLAMBDA_SECRET or VLAMBDA_ENCRYPTION_KEY is required for secrets")
    return hashlib.sha256(("vlambda-env:" + raw).encode()).digest()


def _master_key() -> bytes:
    raw = os.environ.get("VLAMBDA_ENCRYPTION_KEY") or os.environ.get("VLAMBDA_SECRET", "")
    return _key_from(raw)


def _key_id(key: bytes) -> str:
    return hmac.new(key, b"vlambda-env-key-id", hashlib.sha256).hexdigest()[:16]


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(out[:length])


def encrypt(value: str, key: bytes | None = None) -> str:
    key = key or _master_key()
    nonce = secrets.token_bytes(16)
    data = value.encode("utf-8")
    cipher = bytes(a ^ b for a, b in zip(data, _keystream(key, nonce, len(data))))
    tag = hmac.new(key, b"vlambda-env:" + nonce + cipher, hashlib.sha256).digest()
    payload = base64.urlsafe_b64encode(nonce + tag + cipher).decode("ascii")
    return f"v1:{_key_id(key)}:{payload}"


def decrypt(token: str, key: bytes | None = None) -> str:
    key = key or _master_key()
    payload = token
    parts = token.split(":", 2)
    if len(parts) == 3 and parts[0] == "v1":
        _, kid, payload = parts
        if kid != _key_id(key):
            raise ValueError("secret was encrypted with a different key")
    elif len(parts) == 3 and parts[0].startswith("v"):
        raise ValueError("unsupported secret format")
    raw = base64.urlsafe_b64decode(payload.encode("ascii"))
    nonce, tag, cipher = raw[:16], raw[16:48], raw[48:]
    expected = hmac.new(key, b"vlambda-env:" + nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("invalid secret payload")
    data = bytes(a ^ b for a, b in zip(cipher, _keystream(key, nonce, len(cipher))))
    return data.decode("utf-8")


def rotate_secrets(db_path: str, old_key_raw: str, new_key_raw: str) -> dict:
    """Re-encrypt every stored secret from the old key to the new key."""
    if not old_key_raw or not new_key_raw:
        raise ValueError("both the old and new keys are required")
    old_key = _key_from(old_key_raw)
    new_key = _key_from(new_key_raw)
    conn = sqlite3.connect(db_path, timeout=15)
    try:
        mode = os.environ.get("VLAMBDA_SQLITE_JOURNAL", "wal").upper()
        if mode not in ("WAL", "DELETE"):
            mode = "WAL"
        conn.execute(f"PRAGMA journal_mode={mode}")
        conn.execute("PRAGMA busy_timeout=15000")
        rows = conn.execute(
            "SELECT slug, key, value FROM lambda_env WHERE is_secret = 1 ORDER BY slug, key"
        ).fetchall()
        rotated = 0
        already_new = 0
        for slug, key, value in rows:
            try:
                plain = decrypt(value, key=old_key)
            except Exception:
                try:
                    decrypt(value, key=new_key)
                    already_new += 1
                    continue
                except Exception as exc:
                    raise ValueError(f"{slug}/{key}: cannot decrypt with the old key") from exc
            conn.execute(
                "UPDATE lambda_env SET value = ? WHERE slug = ? AND key = ?",
                (encrypt(plain, key=new_key), slug, key),
            )
            rotated += 1
        conn.commit()
    finally:
        conn.close()
    clear_cache()
    return {
        "db": db_path,
        "rotated": rotated,
        "already_new": already_new,
        "total": rotated + already_new,
    }


def _connect():
    from vlambda.dbpool import get as db_get
    return db_get(_db_path())


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
            if not _valid_key(key):
                continue
            if is_secret:
                try:
                    values[key] = decrypt(value)
                except Exception:
                    continue
            else:
                values[key] = value
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
    replace_plain = "vars" in data or "env" in data
    replace_secret = "secrets" in data
    plain = data.get("vars") if "vars" in data else data.get("env", {})
    secret = data.get("secrets", {})
    if not isinstance(plain, dict) or not isinstance(secret, dict):
        return json.dumps({"error": "vars and secrets must be objects"})
    merged: dict[str, tuple[bool, str]] = {}
    try:
        with _connect() as conn:
            owned = conn.execute("SELECT 1 FROM lambdas WHERE slug = ? AND owner_id = ?", (slug, int(owner_id))).fetchone()
            if owned is None:
                return json.dumps({"error": "not found"})
            existing_rows = conn.execute("SELECT key, is_secret, value FROM lambda_env WHERE slug = ?", (slug,)).fetchall()
            existing_secrets = {row[0]: row[2] for row in existing_rows if row[1]}
            if not replace_plain:
                for key, is_secret, value in existing_rows:
                    if not is_secret and _valid_key(key):
                        merged[key] = (False, value)
            if not replace_secret:
                for key, value in existing_secrets.items():
                    if _valid_key(key):
                        merged[key] = (True, value)
            for key, value in plain.items():
                key = str(key)
                if not _valid_key(key):
                    raise ValueError(f"invalid or reserved environment name: {key}")
                merged[key] = (False, str(value))
            for key, value in secret.items():
                key = str(key)
                if not _valid_key(key):
                    raise ValueError(f"invalid or reserved environment name: {key}")
                if value == "********":
                    if key in existing_secrets:
                        merged[key] = (True, existing_secrets[key])
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
    try:
        from vlambda.cache import invalidate_responses
        invalidate_responses()
    except Exception:
        pass
    return list_env(slug, owner_id)
