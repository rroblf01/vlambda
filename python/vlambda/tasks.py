"""Persistent async task queue with cross-replica claiming."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

_MAX_TASKS = 10000
_DLQ_MAX = 1000
_EXECUTOR = None
_EXECUTOR_LOCK = threading.Lock()
_WORKER = None
_WORKER_LOCK = threading.Lock()
_INSTANCE = f"{os.environ.get('VLAMBDA_INSTANCE_ID') or os.uname().nodename}:{os.getpid()}"
_TASK_LEASE = float(os.environ.get("VLAMBDA_TASK_LEASE", "600"))
_MAX_ATTEMPTS = int(os.environ.get("VLAMBDA_TASK_MAX_ATTEMPTS", "5"))


def _db_path() -> str:
    return os.environ.get("VLAMBDA_DB", "./vlambda.db")


def _connect():
    conn = sqlite3.connect(_db_path(), timeout=10)
    mode = os.environ.get("VLAMBDA_SQLITE_JOURNAL", "wal").upper()
    if mode not in ("WAL", "DELETE"):
        mode = "WAL"
    conn.execute(f"PRAGMA journal_mode={mode}")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


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
            "completed REAL,"
            "owner_id INTEGER NOT NULL DEFAULT 0,"
            "slug TEXT NOT NULL DEFAULT '',"
            "code TEXT NOT NULL DEFAULT '',"
            "method TEXT NOT NULL DEFAULT 'GET',"
            "path TEXT NOT NULL DEFAULT '/',"
            "query TEXT NOT NULL DEFAULT '',"
            "body_b64 TEXT NOT NULL DEFAULT '',"
            "request_meta TEXT NOT NULL DEFAULT '{}',"
            "claimed_by TEXT NOT NULL DEFAULT '',"
            "lease_until REAL,"
            "attempts INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS dlq ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "task_id TEXT NOT NULL,"
            "error TEXT NOT NULL,"
            "ts REAL NOT NULL,"
            "owner_id INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dlq_owner ON dlq(owner_id)")
        conn.commit()
        conn.close()
    except Exception:
        pass


_init()


class _TaskWorker:
    def __init__(self, pool):
        self.pool = pool
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(os.environ.get("VLAMBDA_ASYNC_WORKERS", "16"))),
            thread_name_prefix="vlambda-task",
        )
        self._thread = threading.Thread(target=self._loop, name="vlambda-task-worker", daemon=True)

    def start(self):
        self._thread.start()

    def wake(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=5)
        self._executor.shutdown(wait=False)

    def _loop(self):
        from vlambda import lifecycle

        while not self._stop.is_set():
            if not lifecycle.accepting():
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            try:
                task = _claim()
            except Exception:
                task = None
            if task is None:
                self._wake.wait(timeout=2.0)
                self._wake.clear()
                continue
            self._executor.submit(self._execute, task)

    def _execute(self, task: dict):
        from vlambda import lifecycle

        if not lifecycle.begin():
            _release_claim(task["task_id"])
            return
        try:
            _run(
                task["task_id"], task["code"], task["method"], task["path"],
                task["query"], base64.b64decode(task["body_b64"].encode("ascii"))
                if task["body_b64"] else b"",
                self.pool, task["owner_id"], task["slug"],
                task["request_meta"],
            )
        finally:
            lifecycle.end()
            _clear_claim(task["task_id"])


def _ensure_worker(pool):
    global _WORKER
    if _WORKER is None:
        with _WORKER_LOCK:
            if _WORKER is None:
                _WORKER = _TaskWorker(pool)
                _WORKER.start()
            else:
                _WORKER.pool = pool
    else:
        _WORKER.pool = pool
    return _WORKER


def _claim() -> dict | None:
    now = time.time()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT task_id, attempts, owner_id FROM tasks"
            " WHERE status='pending' OR (status='running' AND COALESCE(lease_until, 0) < ?)"
            " ORDER BY created LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        task_id = row[0]
        if row[1] >= _MAX_ATTEMPTS:
            conn.execute(
                "UPDATE tasks SET status='failed', error='too many attempts',"
                " completed=?, claimed_by='', lease_until=NULL WHERE task_id=?",
                (now, task_id),
            )
            conn.commit()
            conn.close()
            _add_dlq(task_id, "too many attempts", row[2])
            return None
        cur = conn.execute(
            "UPDATE tasks SET status='running', claimed_by=?, lease_until=?, attempts=attempts+1"
            " WHERE task_id=? AND (status='pending' OR (status='running' AND COALESCE(lease_until, 0) < ?))",
            (_INSTANCE, now + _TASK_LEASE, task_id, now),
        )
        if cur.rowcount != 1:
            conn.commit()
            return None
        data = conn.execute(
            "SELECT task_id, code, method, path, query, body_b64, owner_id, slug, request_meta"
            " FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        return None
    finally:
        conn.close()
    if data is None:
        return None
    return {
        "task_id": data[0], "code": data[1], "method": data[2], "path": data[3],
        "query": data[4], "body_b64": data[5], "owner_id": data[6],
        "slug": data[7], "request_meta": data[8],
    }


def _release_claim(task_id: str):
    try:
        conn = _connect()
        conn.execute(
            "UPDATE tasks SET status='pending', claimed_by='', lease_until=NULL WHERE task_id=?",
            (task_id,),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _clear_claim(task_id: str):
    try:
        conn = _connect()
        conn.execute(
            "UPDATE tasks SET claimed_by='', lease_until=NULL WHERE task_id=?", (task_id,)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


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
            "INSERT INTO tasks (task_id, status, result, error, created, completed,"
            " owner_id, slug, code, method, path, query, body_b64, request_meta)"
            " VALUES (?, 'pending', NULL, NULL, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id, time.time(), int(owner_id or 0), slug or "", code or "",
                method or "GET", path or "/", query or "",
                base64.b64encode(body or b"").decode("ascii"), request_meta or "{}",
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        return task_id
    _ensure_worker(pool).wake()
    return task_id


def _run(task_id: str, code: str, method: str, path: str, query: str, body: bytes, pool,
         owner_id: int = 0, slug: str = "", request_meta: str = ""):
    retries = int(os.environ.get("VLAMBDA_TASK_RETRIES", "2"))
    delay = float(os.environ.get("VLAMBDA_TASK_RETRY_DELAY", "0.5"))
    last_error = None
    for attempt in range(retries + 1):
        try:
            from vlambda import broker
            from vlambda.env import get_env

            try:
                meta = json.loads(request_meta or "{}")
                if not isinstance(meta, dict):
                    meta = {}
                meta["env"] = get_env(slug) if slug else {}
                request_meta = json.dumps(meta)
            except Exception:
                pass
            enriched_meta, broker_token = broker.attach(request_meta, slug, task_id)
            try:
                result = pool.execute(code, method, path, query, body,
                                      request_id=task_id, slug=slug,
                                      request_meta=enriched_meta)
            finally:
                broker.release(broker_token)
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
            "UPDATE tasks SET status=?, result=COALESCE(?, result), error=COALESCE(?, error),"
            " completed=?, claimed_by='', lease_until=NULL WHERE task_id=?",
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


def get_task(task_id: str, owner_id: int = 0) -> dict | None:
    try:
        owner = int(owner_id or 0)
        conn = _connect()
        row = conn.execute(
            "SELECT task_id, status, result, error, created, completed, attempts"
            " FROM tasks WHERE task_id=? AND (?=0 OR owner_id=?)",
            (task_id, owner, owner),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return {
            "task_id": row[0], "status": row[1], "result": row[2],
            "error": row[3], "created": row[4], "completed": row[5],
            "attempts": row[6],
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
        conn.execute("DELETE FROM tasks WHERE created < ? AND status IN ('completed', 'failed')", (cutoff,))
        conn.execute("DELETE FROM dlq WHERE ts < ?", (cutoff,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def start(pool):
    """Start the queue worker so recovered tasks are claimed at startup."""
    return _ensure_worker(pool)


def stop():
    global _WORKER
    worker, _WORKER = _WORKER, None
    if worker is not None:
        worker.stop()
