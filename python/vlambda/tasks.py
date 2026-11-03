"""Persistent async task tracking and dead-letter queue."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

_MAX_TASKS = 10000
_DLQ_MAX = 1000
_EXECUTOR = None
_EXECUTOR_LOCK = threading.Lock()


def _db_path() -> str:
    return os.environ.get("VLAMBDA_DB", "./vlambda.db")


def _connect():
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _init():
    try:
        conn = _connect()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tasks ("
            "task_id TEXT PRIMARY KEY,"
            "status TEXT NOT NULL,"
            "result TEXT,"
            "error TEXT,"
            "created REAL NOT NULL,"
            "completed REAL"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS dlq ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "task_id TEXT NOT NULL,"
            "error TEXT NOT NULL,"
            "ts REAL NOT NULL"
            ")"
        )
        task_columns = _columns(conn, "tasks")
        if "owner_id" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 0")
        if "slug" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN slug TEXT NOT NULL DEFAULT ''")
        if "owner_id" not in _columns(conn, "dlq"):
            conn.execute("ALTER TABLE dlq ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE tasks SET status='failed', error='interrupted', completed=? WHERE status='pending'",
                     (time.time(),))
        conn.commit()
        conn.close()
    except Exception:
        pass


_init()


def create_task(code: str, method: str, path: str, query: str, body: bytes, pool,
                owner_id: int = 0, slug: str = "", request_meta: str = "") -> str:
    task_id = str(uuid.uuid4())
    cleanup_old(3600)
    try:
        conn = _connect()
        count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        if count >= _MAX_TASKS:
            conn.execute(
                "DELETE FROM tasks WHERE task_id IN ("
                "SELECT task_id FROM tasks WHERE status IN ('completed','failed') ORDER BY created ASC LIMIT ?)",
                (max(1, count - _MAX_TASKS + 1),),
            )
        conn.execute(
            "INSERT INTO tasks (task_id, status, result, error, created, completed, owner_id, slug)"
            " VALUES (?, 'pending', NULL, NULL, ?, NULL, ?, ?)",
            (task_id, time.time(), int(owner_id or 0), slug or ""),
        )
        conn.commit()
        conn.close()
    except Exception:
        return task_id
    _get_executor().submit(_run, task_id, code, method, path, query, body, pool,
                           int(owner_id or 0), slug or "", request_meta or "")
    return task_id


def _run(task_id: str, code: str, method: str, path: str, query: str, body: bytes, pool,
         owner_id: int = 0, slug: str = "", request_meta: str = ""):
    retries = int(os.environ.get("VLAMBDA_TASK_RETRIES", "2"))
    delay = float(os.environ.get("VLAMBDA_TASK_RETRY_DELAY", "0.5"))
    last_error = None
    for attempt in range(retries + 1):
        try:
            result = pool.execute(code, method, path, query, body,
                                  request_id=task_id, slug=slug, request_meta=request_meta)
            if isinstance(result, dict) and set(result.keys()) == {"error"}:
                raise RuntimeError(str(result.get("error")))
            from vlambda.response import serialize
            if not (isinstance(result, dict) and "status" in result):
                result = serialize(result)
            payload = json.dumps(result)
            _update(task_id, "completed", result=payload)
            return
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(delay * (attempt + 1))
    _update(task_id, "failed", error=str(last_error))
    _add_dlq(task_id, str(last_error), owner_id)


def _update(task_id: str, status: str, result: str | None = None, error: str | None = None):
    try:
        conn = _connect()
        conn.execute(
            "UPDATE tasks SET status=?, result=COALESCE(?, result), error=COALESCE(?, error), completed=? WHERE task_id=?",
            (status, result, error, time.time(), task_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _add_dlq(task_id: str, error: str, owner_id: int = 0):
    try:
        conn = _connect()
        count = conn.execute("SELECT COUNT(*) FROM dlq").fetchone()[0]
        if count >= _DLQ_MAX:
            conn.execute("DELETE FROM dlq WHERE id IN (SELECT id FROM dlq ORDER BY id ASC LIMIT ?)",
                         (max(1, count - _DLQ_MAX + 1),))
        conn.execute("INSERT INTO dlq (task_id, error, ts, owner_id) VALUES (?, ?, ?, ?)",
                     (task_id, error, time.time(), int(owner_id or 0)))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _get_executor():
    global _EXECUTOR
    if _EXECUTOR is None:
        with _EXECUTOR_LOCK:
            if _EXECUTOR is None:
                size = int(os.environ.get("VLAMBDA_ASYNC_WORKERS", "16"))
                _EXECUTOR = ThreadPoolExecutor(max_workers=size, thread_name_prefix="vlambda-task")
    return _EXECUTOR


def get_task(task_id: str, owner_id: int = 0) -> dict | None:
    try:
        owner = int(owner_id or 0)
        conn = _connect()
        row = conn.execute(
            "SELECT task_id, status, result, error, created, completed FROM tasks"
            " WHERE task_id=? AND (?=0 OR owner_id=?)",
            (task_id, owner, owner),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return {
            "task_id": row[0], "status": row[1], "result": row[2],
            "error": row[3], "created": row[4], "completed": row[5],
        }
    except Exception:
        return None


def get_dlq(owner_id: int = 0) -> list[dict]:
    try:
        owner = int(owner_id or 0)
        conn = _connect()
        rows = conn.execute(
            "SELECT task_id, error, ts FROM dlq WHERE (?=0 OR owner_id=?) ORDER BY id DESC LIMIT 100",
            (owner, owner),
        ).fetchall()
        conn.close()
        return [{"task_id": r[0], "error": r[1], "ts": r[2]} for r in rows]
    except Exception:
        return []


def cleanup_old(max_age: int = 3600):
    cutoff = time.time() - max_age
    try:
        conn = _connect()
        conn.execute("DELETE FROM tasks WHERE created < ?", (cutoff,))
        conn.execute("DELETE FROM dlq WHERE ts < ?", (cutoff,))
        conn.commit()
        conn.close()
    except Exception:
        pass
