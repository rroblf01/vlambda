"""JWT authentication for vlambda."""

from __future__ import annotations

import hashlib
import hmac
import json
import base64
import time
import os

SECRET = os.environ.get("VLAMBDA_SECRET", "change-me-in-production")


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def create_token(user_id: int, username: str, expires_in: int = 3600) -> str:
    header = _b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64encode(json.dumps({
        "sub": str(user_id),
        "username": username,
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
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{h}"


def check_password(password: str, stored: str) -> bool:
    if ":" not in stored:
        return False
    salt, h = stored.split(":", 1)
    return hmac.compare_digest(hashlib.sha256((salt + password).encode()).hexdigest(), h)
