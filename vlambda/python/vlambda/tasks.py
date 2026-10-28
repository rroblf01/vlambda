"""Async task tracking for vlambda."""

from __future__ import annotations

import threading
import uuid
import time
from typing import Any

_tasks: dict[str, dict] = {}
_lock = threading.Lock()
_MAX_TASKS = 10000


def create_task(code: str, method: str, path: str, query: str, body: bytes, pool) -> str:
    task_id = str(uuid.uuid4())
    with _lock:
        if len(_tasks) >= _MAX_TASKS:
            # Evict oldest completed/failed
            oldest = None
            for tid, t in _tasks.items():
                if t["status"] in ("completed", "failed"):
                    if oldest is None or t["created"] < _tasks[oldest]["created"]:
                        oldest = tid
            if oldest:
                del _tasks[oldest]
        _tasks[task_id] = {
            "status": "pending",
            "result": None,
            "error": None,
            "created": time.time(),
            "completed": None,
        }
    # Execute in background thread
    def _run():
        try:
            result = pool.execute(code, method, path, query, body)
            with _lock:
                _tasks[task_id]["status"] = "completed"
                _tasks[task_id]["result"] = result
                _tasks[task_id]["completed"] = time.time()
        except Exception as e:
            with _lock:
                _tasks[task_id]["status"] = "failed"
                _tasks[task_id]["error"] = str(e)
                _tasks[task_id]["completed"] = time.time()
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return task_id


def get_task(task_id: str) -> dict | None:
    with _lock:
        t = _tasks.get(task_id)
        if t is None:
            return None
        return dict(t)


def cleanup_old(max_age: int = 3600):
    """Remove tasks older than max_age seconds."""
    now = time.time()
    with _lock:
        to_remove = [tid for tid, t in _tasks.items() if now - t["created"] > max_age]
        for tid in to_remove:
            del _tasks[tid]
