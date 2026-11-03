"""JWT authentication for vlambda."""

from __future__ import annotations

import hashlib
import hmac
import json
import base64
import time
import os

SECRET = os.environ.get("VLAMBDA_SECRET", "change-me-in-production")
_PBKDF2_ITERATIONS = int(os.environ.get("VLAMBDA_PBKDF2_ITERATIONS", "200000"))


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def new_auth_epoch() -> str:
    """Random per-user value that invalidates old tokens after a DB reset."""
    return os.urandom(16).hex()


def create_token(user_id: int, username: str, auth_epoch: str = "", expires_in: int = 3600) -> str:
    header = _b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64encode(json.dumps({
        "sub": str(user_id),
        "username": username,
        "epoch": auth_epoch,
        "exp": int(time.time()) + expires_in,
    }).encode())
    sig_input = f"{header}.{payload}".encode()
    sig = hmac.new(SECRET.encode(), sig_input, hashlib.sha256).digest()
    signature = _b64encode(sig)
    return f"{header}.{payload}.{signature}"


def verify_token(token: str) -> dict | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header, payload, signature = parts
    sig_input = f"{header}.{payload}".encode()
    expected_sig = _b64encode(hmac.new(SECRET.encode(), sig_input, hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected_sig):
        return None
    try:
        data = json.loads(_b64decode(payload))
    except Exception:
        return None
    if data.get("exp", 0) < time.time():
        return None
    return data


def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2:{_PBKDF2_ITERATIONS}:{salt}:{digest}"


def check_password(password: str, stored: str) -> bool:
    parts = stored.split(":")
    if len(parts) == 4 and parts[0] == "pbkdf2":
        try:
            iterations = max(1, min(int(parts[1]), 2_000_000))
            salt = bytes.fromhex(parts[2])
        except (ValueError, TypeError):
            return False
        expected = parts[3]
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt, iterations
        ).hex()
        return hmac.compare_digest(digest, expected)
    # Legacy unsalted-iteration SHA-256 hashes; valid only until the next reset
    # or password change.
    if len(parts) == 2:
        salt, expected = parts
        digest = hashlib.sha256((salt + password).encode()).hexdigest()
        return hmac.compare_digest(digest, expected)
    return False
