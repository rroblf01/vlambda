"""SQLite connection pool for vlambda."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager

_pools: dict[str, list] = {}
_lock = threading.Lock()
_MAX = 8


def _make_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    mode = os.environ.get("VLAMBDA_SQLITE_JOURNAL", "wal").upper()
    if mode not in ("WAL", "DELETE"):
        mode = "WAL"
    conn.execute(f"PRAGMA journal_mode={mode}")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init(db_path: str):
    """Pre-create connections for a database path."""
    with _lock:
        pool = _pools.setdefault(db_path, [])
        while len(pool) < _MAX:
            pool.append(_make_conn(db_path))


@contextmanager
def get(db_path: str):
    with _lock:
        pool = _pools.setdefault(db_path, [])
        conn = pool.pop() if pool else _make_conn(db_path)
    try:
        yield conn
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        with _lock:
            pool = _pools.setdefault(db_path, [])
            if len(pool) < _MAX:
                pool.append(conn)
            else:
                conn.close()


def close_all():
    with _lock:
        for pool in _pools.values():
            for conn in pool:
                try:
                    conn.close()
                except Exception:
                    pass
        _pools.clear()
