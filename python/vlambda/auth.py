"""JWT authentication for vlambda."""

from __future__ import annotations

import hashlib
import hmac
import json
import base64
import time
import os
import threading

SECRET = os.environ.get("VLAMBDA_SECRET", "change-me-in-production")
_PBKDF2_ITERATIONS = int(os.environ.get("VLAMBDA_PBKDF2_ITERATIONS", "600000"))
_PASSWORD_MIN_LENGTH = int(os.environ.get("VLAMBDA_PASSWORD_MIN_LENGTH", "12"))
_PASSWORD_MAX_LENGTH = 1024


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


_REGISTER_MAX = int(os.environ.get("VLAMBDA_REGISTER_MAX", "5"))
_REGISTER_WINDOW = float(os.environ.get("VLAMBDA_REGISTER_WINDOW", "600"))
_register_attempts: dict[str, list] = {}
_register_lock = threading.Lock()


def password_error(password: str) -> str:
    if len(password) < _PASSWORD_MIN_LENGTH:
        return f"password must be at least {_PASSWORD_MIN_LENGTH} characters"
    if len(password) > _PASSWORD_MAX_LENGTH:
        return "password is too long"
    return ""


def registration_allowed(remote_ip: str = "") -> bool:
    """Fixed-window registration limit per source IP."""
    now = time.time()
    key = str(remote_ip or "unknown")[:64]
    with _register_lock:
        entry = _register_attempts.get(key)
        if entry is None or now >= entry[1]:
            _register_attempts[key] = [1, now + _REGISTER_WINDOW]
            return True
        if entry[0] >= _REGISTER_MAX:
            return False
        entry[0] += 1
        return True


_LOGIN_MAX_FAILURES = int(os.environ.get("VLAMBDA_LOGIN_MAX_FAILURES", "5"))
_LOGIN_WINDOW = float(os.environ.get("VLAMBDA_LOGIN_WINDOW", "300"))
_LOGIN_BLOCK = float(os.environ.get("VLAMBDA_LOGIN_BLOCK", "300"))
_login_failures: dict[tuple[str, str], list] = {}
_login_lock = threading.Lock()


def _login_key(username: str, remote_ip: str) -> tuple[str, str]:
    return (str(username)[:128], str(remote_ip or "unknown")[:64])


def login_allowed(username: str, remote_ip: str = "") -> bool:
    """Return whether a login attempt may proceed without checking the password."""
    now = time.time()
    key = _login_key(username, remote_ip)
    with _login_lock:
        entry = _login_failures.get(key)
        if entry is None:
            return True
        count, first, blocked_until = entry
        if blocked_until > now:
            return False
        if now - first > _LOGIN_WINDOW:
            _login_failures.pop(key, None)
            return True
        return True


def record_login_failure(username: str, remote_ip: str = "") -> None:
    now = time.time()
    key = _login_key(username, remote_ip)
    with _login_lock:
        entry = _login_failures.get(key)
        if entry is None or now - entry[1] > _LOGIN_WINDOW:
            entry = [1, now, 0.0]
        else:
            entry[0] += 1
        if entry[0] >= _LOGIN_MAX_FAILURES:
            entry[2] = now + _LOGIN_BLOCK
        _login_failures[key] = entry
        if len(_login_failures) > 4096:
            cutoff = now - _LOGIN_WINDOW
            for candidate in [k for k, v in _login_failures.items() if v[1] < cutoff and v[2] < now]:
                _login_failures.pop(candidate, None)


def record_login_success(username: str, remote_ip: str = "") -> None:
    with _login_lock:
        _login_failures.pop(_login_key(username, remote_ip), None)


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
