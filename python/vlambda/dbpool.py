"""SQLite connection pool for vlambda."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

_pool: list = []
_lock = threading.Lock()
_MAX = 8


def _make_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init(db_path: str):
    """Pre-create connections."""
    with _lock:
        while len(_pool) < _MAX:
            _pool.append(_make_conn(db_path))


@contextmanager
def get(db_path: str):
    with _lock:
        if _pool:
            conn = _pool.pop()
        else:
            conn = _make_conn(db_path)
    try:
        yield conn
    finally:
        with _lock:
            if len(_pool) < _MAX:
                _pool.append(conn)
            else:
                conn.close()


def close_all():
    with _lock:
        for c in _pool:
            c.close()
        _pool.clear()
