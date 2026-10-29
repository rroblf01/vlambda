"""Optional shared response cache for horizontal scaling.

The default backend is in-process. A file backend can be shared between
processes on the same node, and a Redis backend is used when `redis` is
installed and `VLAMBDA_REDIS_URL` is set.
"""

from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

_BACKEND = os.environ.get("VLAMBDA_CACHE_BACKEND", "memory").lower()
_REDIS_URL = os.environ.get("VLAMBDA_REDIS_URL", "")
_CACHE_DIR = os.environ.get("VLAMBDA_SHARED_CACHE_DIR", "/tmp/opencode/vlambda_shared_cache")
_MAX_ENTRIES = int(os.environ.get("VLAMBDA_RESP_CACHE", "256"))
_TTL = int(os.environ.get("VLAMBDA_RESP_CACHE_TTL", "5"))

_memory: OrderedDict[str, tuple[Any, float]] = OrderedDict()
_redis = None


def _now() -> float:
    return time.time()


def _redis_client():
    global _redis
    if _redis is not None:
        return _redis
    if _BACKEND != "redis" or not _REDIS_URL:
        return None
    try:
        import redis

        _redis = redis.Redis.from_url(_REDIS_URL, decode_responses=True)
        _redis.ping()
    except Exception:
        _redis = None
    return _redis


def get(key: str) -> Any | None:
    client = _redis_client()
    if client is not None:
        try:
            raw = client.get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception:
            return None

    if _BACKEND == "file":
        path = Path(_CACHE_DIR) / (key.encode().hex() + ".json")
        try:
            if not path.exists():
                return None
            data = json.loads(path.read_text())
            if data.get("expires", 0) < _now():
                path.unlink(missing_ok=True)
                return None
            return data.get("value")
        except Exception:
            return None

    entry = _memory.get(key)
    if entry is None:
        return None
    value, expires = entry
    if _now() > expires:
        _memory.pop(key, None)
        return None
    _memory.move_to_end(key)
    return value


def set(key: str, value: Any, ttl: int | None = None):
    if ttl is None:
        ttl = _TTL
    client = _redis_client()
    if client is not None:
        try:
            client.setex(key, ttl, json.dumps(value))
        except Exception:
            pass
        return

    if _BACKEND == "file":
        path = Path(_CACHE_DIR)
        path.mkdir(parents=True, exist_ok=True)
        try:
            (path / (key.encode().hex() + ".json")).write_text(
                json.dumps({"value": value, "expires": _now() + ttl})
            )
        except Exception:
            pass
        return

    if key in _memory:
        _memory.move_to_end(key)
    _memory[key] = (value, _now() + ttl)
    if len(_memory) > _MAX_ENTRIES:
        _memory.popitem(last=False)


def clear():
    client = _redis_client()
    if client is not None:
        try:
            client.flushdb()
        except Exception:
            pass
        return
    if _BACKEND == "file":
        try:
            for p in Path(_CACHE_DIR).glob("*.json"):
                p.unlink(missing_ok=True)
        except Exception:
            pass
        return
    _memory.clear()
