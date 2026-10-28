"""LRU cache for compiled lambda code objects and sandbox templates."""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

from vlambda.sandbox import make_sandbox

_MAX_SIZE = 1024
_cache: OrderedDict[str, tuple] = OrderedDict()
_lock = threading.Lock()


def get(code: str):
    with _lock:
        if code in _cache:
            _cache.move_to_end(code)
            return _cache[code]
    # Compile outside lock
    compiled, ns_template = make_sandbox(code, None)
    with _lock:
        if code in _cache:
            _cache.move_to_end(code)
            return _cache[code]
        if len(_cache) >= _MAX_SIZE:
            _cache.popitem(last=False)
        _cache[code] = (compiled, ns_template)
        return _cache[code]


def prewarm(code: str):
    """Pre-compile code without returning it."""
    get(code)


def invalidate(code: str):
    with _lock:
        _cache.pop(code, None)


def clear():
    with _lock:
        _cache.clear()


def size() -> int:
    with _lock:
        return len(_cache)
