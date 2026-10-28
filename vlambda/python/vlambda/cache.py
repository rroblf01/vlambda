"""LRU cache for compiled lambda code objects and sandbox templates."""

from __future__ import annotations

import threading
import marshal
import os
from collections import OrderedDict
from typing import Any

from vlambda.sandbox import make_sandbox

_MAX_SIZE = 1024
_CACHE_DIR = os.environ.get("VLAMBDA_CACHE_DIR", "/tmp/opencode/vlambda_cache")
_cache: OrderedDict[str, tuple] = OrderedDict()
_lock = threading.Lock()


def _disk_path(code: str) -> str:
    import hashlib
    h = hashlib.sha256(code.encode()).hexdigest()[:16]
    return os.path.join(_CACHE_DIR, f"{h}.pyc")


def _load_from_disk(code: str):
    path = _disk_path(code)
    try:
        with open(path, "rb") as f:
            return marshal.load(f)
    except (FileNotFoundError, EOFError, ValueError, TypeError):
        return None


def _save_to_disk(code: str, compiled):
    path = _disk_path(code)
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(path, "wb") as f:
            marshal.dump(compiled, f)
    except Exception:
        pass


def get(code: str):
    with _lock:
        if code in _cache:
            _cache.move_to_end(code)
            return _cache[code]
    # Try disk cache
    compiled = _load_from_disk(code)
    if compiled is None:
        compiled, ns_template = make_sandbox(code, None)
        _save_to_disk(code, compiled)
    else:
        _, ns_template = make_sandbox(code, None)
    with _lock:
        if code in _cache:
            _cache.move_to_end(code)
            return _cache[code]
        if len(_cache) >= _MAX_SIZE:
            _cache.popitem(last=False)
        _cache[code] = (compiled, ns_template)
        return _cache[code]


def prewarm(code: str):
    get(code)


def invalidate(code: str):
    with _lock:
        _cache.pop(code, None)
    try:
        os.remove(_disk_path(code))
    except FileNotFoundError:
        pass


def clear():
    with _lock:
        _cache.clear()


def size() -> int:
    with _lock:
        return len(_cache)


# Response cache for idempotent GET requests
import time as _time
_RESP_CACHE: dict = {}
_RESP_CACHE_LOCK = threading.Lock()
_RESP_CACHE_TTL = 5  # seconds
_RESP_CACHE_MAX = 256


def get_response(key: str):
    with _RESP_CACHE_LOCK:
        entry = _RESP_CACHE.get(key)
        if entry is None:
            return None
        val, expires = entry
        if _time.time() > expires:
            del _RESP_CACHE[key]
            return None
        return val


def set_response(key: str, value):
    with _RESP_CACHE_LOCK:
        if len(_RESP_CACHE) >= _RESP_CACHE_MAX:
            # Evict oldest
            oldest = min(_RESP_CACHE, key=lambda k: _RESP_CACHE[k][1])
            del _RESP_CACHE[oldest]
        _RESP_CACHE[key] = (value, _time.time() + _RESP_CACHE_TTL)


def invalidate_responses():
    with _RESP_CACHE_LOCK:
        _RESP_CACHE.clear()
