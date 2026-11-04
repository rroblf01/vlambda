"""LRU cache for compiled lambda code objects and sandbox templates."""

from __future__ import annotations

import threading
import marshal
import os
import sys
import tempfile
import types
from collections import OrderedDict
from typing import Any

from vlambda.sandbox import make_sandbox, make_namespace

_MAX_SIZE = int(__import__("os").environ.get("VLAMBDA_CODE_CACHE", "1024"))
_CACHE_DIR = os.environ.get("VLAMBDA_CACHE_DIR", "/tmp/opencode/vlambda_cache")
# Code objects are only portable between interpreters with the same magic
# number and ABI.  A shared cache directory could otherwise feed a GIL build a
# code object marshalled by a free-threaded build, which crashes the eval loop.
_CACHE_TAG = "-".join(part for part in (sys.implementation.cache_tag, sys.abiflags) if part) or "python"
_cache: OrderedDict[str, tuple] = OrderedDict()
_lock = threading.Lock()


def _cache_root() -> str:
    return os.path.join(_CACHE_DIR, _CACHE_TAG)


def _disk_path(code: str) -> str:
    import hashlib
    h = hashlib.sha256(code.encode()).hexdigest()[:16]
    return os.path.join(_cache_root(), f"{h}.pyc")


def _load_from_disk(code: str):
    path = _disk_path(code)
    try:
        with open(path, "rb") as f:
            compiled = marshal.load(f)
    except (FileNotFoundError, EOFError, ValueError, TypeError, OSError):
        return None
    return compiled if isinstance(compiled, types.CodeType) else None


def _save_to_disk(code: str, compiled):
    path = _disk_path(code)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".pyc")
    except OSError:
        return
    try:
        with os.fdopen(fd, "wb") as f:
            marshal.dump(compiled, f)
            f.flush()
            os.fsync(f.fileno())
        # Readers can run concurrently with the writer; publish atomically.
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def get(code: str):
    with _lock:
        if code in _cache:
            _cache.move_to_end(code)
            return _cache[code]
    # Try disk cache
    compiled = _load_from_disk(code)
    if compiled is None:
        compiled = compile(code, "<lambda>", "exec", optimize=2)
        _save_to_disk(code, compiled)
    ns_template = make_namespace(None)
    exec(compiled, ns_template)
    handler = ns_template.get("handler")
    with _lock:
        if code in _cache:
            _cache.move_to_end(code)
            return _cache[code]
        if len(_cache) >= _MAX_SIZE:
            _cache.popitem(last=False)
        _cache[code] = (compiled, ns_template, handler)
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


# Response cache for idempotent GET requests.
# Backed by the optional shared cache for horizontal scaling.
from vlambda.shared_cache import get as _shared_get, set as _shared_set, clear as _shared_clear


def get_response(key: str):
    return _shared_get(key)


def set_response(key: str, value):
    _shared_set(key, value)


def invalidate_responses():
    _shared_clear()
