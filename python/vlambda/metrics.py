"""Persistent, batched metrics storage for vlambda.

Metrics are queued in memory and flushed to a separate SQLite database by a
background thread, so request handling is not blocked by disk writes.
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
from typing import Any

_DB_PATH = os.environ.get("VLAMBDA_METRICS_DB", "./vlambda_metrics.db")
_RETENTION_DAYS = int(os.environ.get("VLAMBDA_METRICS_RETENTION", "7"))
_BATCH_SIZE = int(os.environ.get("VLAMBDA_METRICS_BATCH", "100"))
_FLUSH_INTERVAL = float(os.environ.get("VLAMBDA_METRICS_FLUSH", "1.0"))

_queue: "queue.Queue[tuple]" = queue.Queue(maxsize=10000)
_conn: sqlite3.Connection | None = None
_lock = threading.Lock()
_stop = threading.Event()
_worker: threading.Thread | None = None
_last_cleanup = 0.0


def init(db_path: str | None = None, retention_days: int | None = None):
    global _DB_PATH, _RETENTION_DAYS, _conn, _worker, _last_cleanup
    if db_path is not None:
        _DB_PATH = db_path
    if retention_days is not None:
        _RETENTION_DAYS = retention_days
    os.makedirs(os.path.dirname(os.path.abspath(_DB_PATH)), exist_ok=True)
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
            _conn.execute(
                "CREATE TABLE IF NOT EXISTS metrics ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "ts REAL NOT NULL,"
                "slug TEXT NOT NULL,"
                "method TEXT NOT NULL,"
                "path TEXT NOT NULL,"
                "latency_ms REAL NOT NULL,"
                "error TEXT NOT NULL DEFAULT ''"
                ")"
            )
            _conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts)")
            _conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_slug ON metrics(slug)")
            _conn.commit()
    if _worker is None or not _worker.is_alive():
        _stop.clear()
        _worker = threading.Thread(target=_run, daemon=True)
        _worker.start()
    _last_cleanup = time.time()


def record(slug: str, method: str, path: str, latency_ms: float, error: str = ""):
    try:
        _queue.put_nowait((time.time(), slug, method, path, latency_ms, error))
    except queue.Full:
        pass


def _flush(batch: list[tuple]):
    if not batch or _conn is None:
        return
    with _lock:
        _conn.executemany(
            "INSERT INTO metrics (ts, slug, method, path, latency_ms, error) VALUES (?, ?, ?, ?, ?, ?)",
            batch,
        )
        _conn.commit()


def _cleanup():
    global _last_cleanup
    if _conn is None:
        return
    cutoff = time.time() - _RETENTION_DAYS * 86400
    with _lock:
        _conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
        _conn.commit()
    _last_cleanup = time.time()


def _run():
    global _last_cleanup
    batch: list[tuple] = []
    last_flush = time.time()
    while not _stop.is_set():
        try:
            item = _queue.get(timeout=_FLUSH_INTERVAL)
            batch.append(item)
        except queue.Empty:
            pass
        now = time.time()
        if len(batch) >= _BATCH_SIZE or (batch and now - last_flush >= _FLUSH_INTERVAL):
            _flush(batch)
            batch = []
            last_flush = now
            if now - _last_cleanup >= 3600:
                _cleanup()
    if batch:
        _flush(batch)


def query(days: int = 1, slug: str | None = None) -> dict[str, Any]:
    if _conn is None:
        return {"days": days, "items": []}
    cutoff = time.time() - days * 86400
    sql = (
        "SELECT ts, slug, method, path, latency_ms, error FROM metrics "
        "WHERE ts >= ?"
    )
    params: list[Any] = [cutoff]
    if slug:
        sql += " AND slug = ?"
        params.append(slug)
    sql += " ORDER BY ts DESC LIMIT 10000"
    with _lock:
        rows = _conn.execute(sql, params).fetchall()
    items = [
        {
            "ts": r[0],
            "slug": r[1],
            "method": r[2],
            "path": r[3],
            "latency_ms": r[4],
            "error": r[5],
        }
        for r in rows
    ]
    return {"days": days, "count": len(items), "items": items}


def stats(days: int = 1, slug: str | None = None) -> dict[str, Any]:
    if _conn is None:
        return {"days": days, "count": 0, "avg_ms": 0, "p50_ms": 0, "p95_ms": 0, "p99_ms": 0, "errors": 0}
    cutoff = time.time() - days * 86400
    sql = "SELECT latency_ms, error FROM metrics WHERE ts >= ?"
    params: list[Any] = [cutoff]
    if slug:
        sql += " AND slug = ?"
        params.append(slug)
    with _lock:
        rows = _conn.execute(sql, params).fetchall()
    latencies = sorted(r[0] for r in rows)
    errors = sum(1 for r in rows if r[1])
    n = len(latencies)
    if n == 0:
        return {"days": days, "count": 0, "avg_ms": 0, "p50_ms": 0, "p95_ms": 0, "p99_ms": 0, "errors": 0}

    def pct(p: float) -> float:
        idx = min(n - 1, int((n - 1) * p))
        return latencies[idx]

    return {
        "days": days,
        "count": n,
        "avg_ms": sum(latencies) / n,
        "p50_ms": pct(0.50),
        "p95_ms": pct(0.95),
        "p99_ms": pct(0.99),
        "errors": errors,
    }


def close():
    _stop.set()
    if _worker is not None and _worker.is_alive():
        _worker.join(timeout=5)
