"""Persistent, batched metrics storage for vlambda.

Metrics are queued in memory and flushed to a separate SQLite database by a
background thread, so request handling is not blocked by disk writes.
"""

from __future__ import annotations

import csv
import io
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
_logs_queue: "queue.Queue[tuple]" = queue.Queue(maxsize=10000)
_conn: sqlite3.Connection | None = None
_CONN_PATH: str | None = None
_lock = threading.Lock()
_stop = threading.Event()
_worker: threading.Thread | None = None
_last_cleanup = 0.0
_last_rollup = 0.0
_seen_slugs: set[str] = set()


def init(db_path: str | None = None, retention_days: int | None = None):
    global _DB_PATH, _RETENTION_DAYS, _conn, _CONN_PATH, _worker, _last_cleanup, _last_rollup
    if db_path is not None:
        _DB_PATH = db_path
    if retention_days is not None:
        _RETENTION_DAYS = retention_days
    target = os.path.abspath(_DB_PATH)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with _lock:
        if _conn is not None and _CONN_PATH != target:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
        if _conn is None:
            _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
            _CONN_PATH = target
            try:
                _conn.execute("ALTER TABLE metrics ADD COLUMN request_id TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            try:
                _conn.execute("ALTER TABLE metrics ADD COLUMN error_type TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            try:
                _conn.execute("ALTER TABLE metrics ADD COLUMN cold_start INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            _conn.execute(
                "CREATE TABLE IF NOT EXISTS metrics ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "ts REAL NOT NULL,"
                "request_id TEXT NOT NULL DEFAULT '',"
                "slug TEXT NOT NULL,"
                "method TEXT NOT NULL,"
                "path TEXT NOT NULL,"
                "latency_ms REAL NOT NULL,"
                "error TEXT NOT NULL DEFAULT '',"
                "error_type TEXT NOT NULL DEFAULT '',"
                "cold_start INTEGER NOT NULL DEFAULT 0"
                ")"
            )
            _conn.execute(
                "CREATE TABLE IF NOT EXISTS logs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "ts REAL NOT NULL,"
                "request_id TEXT NOT NULL DEFAULT '',"
                "slug TEXT NOT NULL,"
                "level TEXT NOT NULL DEFAULT 'info',"
                "message TEXT NOT NULL"
                ")"
            )
            _conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts)")
            _conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_slug ON logs(slug)")
            _conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts)")
            _conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_slug ON metrics(slug)")
            _conn.execute(
                "CREATE TABLE IF NOT EXISTS metrics_daily ("
                "day TEXT NOT NULL,"
                "slug TEXT NOT NULL,"
                "requests INTEGER NOT NULL,"
                "errors INTEGER NOT NULL,"
                "avg_latency_ms REAL NOT NULL,"
                "p95_latency_ms REAL NOT NULL,"
                "cold_starts INTEGER NOT NULL,"
                "PRIMARY KEY (day, slug)"
                ")"
            )
            _conn.commit()
    if _worker is None or not _worker.is_alive():
        _stop.clear()
        _worker = threading.Thread(target=_run, daemon=True)
        _worker.start()
    _last_cleanup = time.time()
    _last_rollup = time.time()


def record(request_id: str, slug: str, method: str, path: str, latency_ms: float, error: str = ""):
    cold_start = 0
    if slug not in _seen_slugs:
        _seen_slugs.add(slug)
        cold_start = 1
    error_type = error.split(":")[0].split()[0] if error else ""
    try:
        _queue.put_nowait((time.time(), request_id, slug, method, path, latency_ms, error, error_type, cold_start))
    except queue.Full:
        pass


def record_log(request_id: str, slug: str, level: str, message: str):
    try:
        _logs_queue.put_nowait((time.time(), request_id, slug, level, str(message)))
    except queue.Full:
        pass


def _flush_logs(batch: list[tuple]):
    if not batch or _conn is None:
        return
    with _lock:
        _conn.executemany(
            "INSERT INTO logs (ts, request_id, slug, level, message) VALUES (?, ?, ?, ?, ?)",
            batch,
        )
        _conn.commit()


def _flush(batch: list[tuple]):
    if not batch or _conn is None:
        return
    with _lock:
        _conn.executemany(
            "INSERT INTO metrics (ts, request_id, slug, method, path, latency_ms, error, error_type, cold_start) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            batch,
        )
        _conn.commit()


def _rollup():
    global _last_rollup
    if _conn is None:
        return
    cutoff = time.time() - 86400
    with _lock:
        rows = _conn.execute(
            "SELECT substr(datetime(ts, 'unixepoch'), 1, 10), slug, latency_ms, error, cold_start FROM metrics WHERE ts < ?",
            (cutoff,),
        ).fetchall()
    if not rows:
        _last_rollup = time.time()
        return
    grouped: dict[tuple[str, str], list[tuple]] = {}
    for day, slug, latency, error, cold in rows:
        grouped.setdefault((day, slug), []).append((latency, error, cold))
    with _lock:
        for (day, slug), values in grouped.items():
            latencies = sorted(v[0] for v in values)
            n = len(latencies)
            p95 = latencies[min(n - 1, int((n - 1) * 0.95))] if n else 0.0
            _conn.execute(
                "INSERT INTO metrics_daily (day, slug, requests, errors, avg_latency_ms, p95_latency_ms, cold_starts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(day, slug) DO UPDATE SET "
                "requests=requests+excluded.requests, errors=errors+excluded.errors, "
                "avg_latency_ms=(avg_latency_ms*requests+excluded.avg_latency_ms*excluded.requests)/(requests+excluded.requests), "
                "p95_latency_ms=excluded.p95_latency_ms, cold_starts=cold_starts+excluded.cold_starts",
                (
                    day,
                    slug,
                    n,
                    sum(1 for v in values if v[1]),
                    sum(v[0] for v in values) / n,
                    p95,
                    sum(v[2] for v in values),
                ),
            )
        _conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
        _conn.commit()
    _last_rollup = time.time()


def _cleanup():
    global _last_cleanup
    if _conn is None:
        return
    cutoff = time.time() - _RETENTION_DAYS * 86400
    with _lock:
        _conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
        _conn.execute("DELETE FROM metrics_daily WHERE day < date(?, 'unixepoch')", (cutoff,))
        _conn.commit()
    _last_cleanup = time.time()


def _run():
    global _last_cleanup, _last_rollup
    batch: list[tuple] = []
    log_batch: list[tuple] = []
    last_flush = time.time()
    while not _stop.is_set():
        try:
            item = _queue.get(timeout=_FLUSH_INTERVAL)
            batch.append(item)
        except queue.Empty:
            pass
        now = time.time()
        while True:
            try:
                log_batch.append(_logs_queue.get_nowait())
            except queue.Empty:
                break
        if len(batch) >= _BATCH_SIZE or len(log_batch) >= _BATCH_SIZE or (batch and now - last_flush >= _FLUSH_INTERVAL) or (log_batch and now - last_flush >= _FLUSH_INTERVAL):
            _flush(batch)
            _flush_logs(log_batch)
            batch = []
            log_batch = []
            last_flush = now
            if now - _last_rollup >= 3600:
                _rollup()
            if now - _last_cleanup >= 3600:
                _cleanup()
    # Drain anything queued between the last loop iteration and stop.
    while True:
        try:
            batch.append(_queue.get_nowait())
        except queue.Empty:
            break
    while True:
        try:
            log_batch.append(_logs_queue.get_nowait())
        except queue.Empty:
            break
    if batch:
        _flush(batch)
    if log_batch:
        _flush_logs(log_batch)
    _rollup()
    _cleanup()


def query(days: int = 1, slug: str | None = None) -> dict[str, Any]:
    if _conn is None:
        return {"days": days, "count": 0, "items": []}
    cutoff = time.time() - days * 86400
    sql = (
        "SELECT ts, request_id, slug, method, path, latency_ms, error, error_type, cold_start FROM metrics "
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
            "request_id": r[1],
            "slug": r[2],
            "method": r[3],
            "path": r[4],
            "latency_ms": r[5],
            "error": r[6],
            "error_type": r[7],
            "cold_start": bool(r[8]),
        }
        for r in rows
    ]
    return {"days": days, "count": len(items), "items": items}


def summary(days: int = 1, slug: str | None = None) -> dict[str, Any]:
    if _conn is None:
        return {"days": days, "items": []}
    cutoff = time.time() - days * 86400
    cutoff_day = time.strftime("%Y-%m-%d", time.gmtime(cutoff))
    sql = (
        "SELECT day, slug, requests, errors, avg_latency_ms, p95_latency_ms, cold_starts "
        "FROM metrics_daily WHERE day >= ?"
    )
    params: list[Any] = [cutoff_day]
    if slug:
        sql += " AND slug = ?"
        params.append(slug)
    sql += " ORDER BY day DESC, slug"
    with _lock:
        rows = _conn.execute(sql, params).fetchall()
    items = [
        {
            "day": r[0],
            "slug": r[1],
            "requests": r[2],
            "errors": r[3],
            "avg_latency_ms": r[4],
            "p95_latency_ms": r[5],
            "cold_starts": r[6],
        }
        for r in rows
    ]
    return {"days": days, "count": len(items), "items": items}


def export_csv(days: int = 1, slug: str | None = None) -> str:
    data = query(days, slug)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["ts", "request_id", "slug", "method", "path", "latency_ms", "error", "error_type", "cold_start"])
    for item in data["items"]:
        writer.writerow(
            [
                item["ts"],
                item["request_id"],
                item["slug"],
                item["method"],
                item["path"],
                item["latency_ms"],
                item["error"],
                item["error_type"],
                int(item["cold_start"]),
            ]
        )
    return out.getvalue()


def aggregate(days: int = 1, slug: str | None = None) -> dict[str, Any]:
    """Aggregate persisted metrics per lambda, including daily rollups."""
    if _conn is None:
        return {"days": days, "requests_total": 0, "errors_total": 0, "cold_starts": 0, "per_lambda": []}
    cutoff = time.time() - days * 86400
    cutoff_day = time.strftime("%Y-%m-%d", time.gmtime(cutoff))
    combined: dict[str, dict[str, float]] = {}

    def add(key: str, invocations: float, errors: float, latency_sum: float, cold_starts: float):
        item = combined.setdefault(key, {"invocations": 0.0, "errors": 0.0, "latency_sum": 0.0, "cold_starts": 0.0})
        item["invocations"] += invocations
        item["errors"] += errors
        item["latency_sum"] += latency_sum
        item["cold_starts"] += cold_starts

    with _lock:
        raw_sql = (
            "SELECT slug, COUNT(*), SUM(CASE WHEN error != '' THEN 1 ELSE 0 END), "
            "AVG(latency_ms), SUM(cold_start) FROM metrics WHERE ts >= ?"
        )
        params: list[Any] = [cutoff]
        if slug:
            raw_sql += " AND slug = ?"
            params.append(slug)
        raw_sql += " GROUP BY slug"
        for key, count, errors, avg_latency, cold in _conn.execute(raw_sql, params).fetchall():
            add(key, float(count), float(errors or 0), float(avg_latency or 0) * float(count), float(cold or 0))

        daily_sql = (
            "SELECT slug, SUM(requests), SUM(errors), SUM(avg_latency_ms * requests), SUM(cold_starts) "
            "FROM metrics_daily WHERE day >= ?"
        )
        dparams: list[Any] = [cutoff_day]
        if slug:
            daily_sql += " AND slug = ?"
            dparams.append(slug)
        daily_sql += " GROUP BY slug"
        for key, requests, errors, latency_sum, cold in _conn.execute(daily_sql, dparams).fetchall():
            add(key, float(requests or 0), float(errors or 0), float(latency_sum or 0), float(cold or 0))

    per_lambda = []
    for key, item in combined.items():
        n = item["invocations"]
        per_lambda.append({
            "slug": key,
            "invocations": int(n),
            "errors": int(item["errors"]),
            "latency_avg_ms": (item["latency_sum"] / n) if n else 0.0,
            "cold_starts": int(item["cold_starts"]),
        })
    per_lambda.sort(key=lambda x: x["invocations"], reverse=True)
    return {
        "days": days,
        "requests_total": int(sum(i["invocations"] for i in per_lambda)),
        "errors_total": int(sum(i["errors"] for i in per_lambda)),
        "cold_starts": int(sum(i["cold_starts"] for i in per_lambda)),
        "per_lambda": per_lambda,
    }


def query_logs(days: int = 1, slug: str | None = None, limit: int = 1000) -> dict[str, Any]:
    if _conn is None:
        return {"days": days, "count": 0, "items": []}
    cutoff = time.time() - days * 86400
    sql = "SELECT ts, request_id, slug, level, message FROM logs WHERE ts >= ?"
    params: list[Any] = [cutoff]
    if slug:
        sql += " AND slug = ?"
        params.append(slug)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(max(1, min(limit, 10000)))
    with _lock:
        rows = _conn.execute(sql, params).fetchall()
    return {
        "days": days,
        "count": len(rows),
        "items": [
            {"ts": r[0], "request_id": r[1], "slug": r[2], "level": r[3], "message": r[4]}
            for r in rows
        ],
    }


def stats(days: int = 1, slug: str | None = None) -> dict[str, Any]:
    if _conn is None:
        return {"days": days, "count": 0, "avg_ms": 0, "p50_ms": 0, "p95_ms": 0, "p99_ms": 0, "errors": 0, "cold_starts": 0}
    cutoff = time.time() - days * 86400
    sql = "SELECT latency_ms, error, cold_start FROM metrics WHERE ts >= ?"
    params: list[Any] = [cutoff]
    if slug:
        sql += " AND slug = ?"
        params.append(slug)
    with _lock:
        rows = _conn.execute(sql, params).fetchall()
    latencies = sorted(r[0] for r in rows)
    errors = sum(1 for r in rows if r[1])
    cold_starts = sum(r[2] for r in rows)
    n = len(latencies)
    if n == 0:
        return {"days": days, "count": 0, "avg_ms": 0, "p50_ms": 0, "p95_ms": 0, "p99_ms": 0, "errors": 0, "cold_starts": 0}

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
        "cold_starts": cold_starts,
    }


def close():
    global _conn
    _stop.set()
    if _worker is not None and _worker.is_alive():
        _worker.join(timeout=5)
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None
