"""Persistent cron schedules and a single-leader scheduler thread."""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

from vlambda.cron import Cron, CronError
from vlambda.dbpool import get as db_get

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
_MAX_PAYLOAD = 64 * 1024
_MAX_SCHEDULES = int(os.environ.get("VLAMBDA_MAX_SCHEDULES", "20"))
_LEASE_TTL = 15.0
_GRACE = float(os.environ.get("VLAMBDA_SCHEDULE_GRACE", "60"))
_RETENTION_DAYS = int(os.environ.get("VLAMBDA_SCHEDULE_RETENTION", "7"))

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT NOT NULL,
        owner_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        cron TEXT NOT NULL,
        timezone TEXT NOT NULL DEFAULT 'UTC',
        method TEXT NOT NULL DEFAULT 'POST',
        path TEXT NOT NULL DEFAULT '/schedule',
        payload TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 1,
        running INTEGER NOT NULL DEFAULT 0,
        run_started REAL,
        next_run REAL,
        last_run REAL,
        last_status TEXT,
        created_at REAL NOT NULL,
        UNIQUE(slug, name)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(enabled, next_run)",
    """CREATE TABLE IF NOT EXISTS schedule_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schedule_id INTEGER NOT NULL,
        slug TEXT NOT NULL,
        request_id TEXT,
        started REAL NOT NULL,
        finished REAL,
        status TEXT NOT NULL,
        error TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_schedule_runs_schedule ON schedule_runs(schedule_id, id)",
    """CREATE TABLE IF NOT EXISTS scheduler_lease (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        owner TEXT NOT NULL,
        expires REAL NOT NULL
    )""",
)

_worker: "Scheduler | None" = None


class ScheduleError(ValueError):
    pass


def _db_path() -> str:
    return os.environ.get("VLAMBDA_DB", "./vlambda.db")


def ensure_schema() -> None:
    with db_get(_db_path()) as conn:
        for statement in _SCHEMA:
            conn.execute(statement)
        conn.commit()


def _owned(conn, slug: str, owner_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM lambdas WHERE slug = ? AND owner_id = ?", (slug, int(owner_id))
    ).fetchone()
    return row is not None


def _validate(data: dict) -> dict:
    name = str(data.get("name") or "").strip()
    if not _NAME_RE.match(name):
        raise ScheduleError("invalid schedule name")
    expression = str(data.get("cron") or "").strip()
    if len(expression) > 200:
        raise ScheduleError("cron expression is too long")
    try:
        cron = Cron(expression)
    except CronError as error:
        raise ScheduleError(str(error)) from error
    timezone = str(data.get("timezone") or "UTC").strip() or "UTC"
    try:
        ZoneInfo(timezone)
    except Exception as error:
        raise ScheduleError(f"invalid timezone: {timezone}") from error
    method = str(data.get("method") or "POST").upper()
    if method not in _METHODS:
        raise ScheduleError("invalid method")
    path = str(data.get("path") or "/schedule").strip() or "/schedule"
    if not path.startswith("/") or len(path) > 2000 or any(char in path for char in ("\r", "\n", "\x00")):
        raise ScheduleError("invalid path")
    payload = data.get("payload", "")
    if payload is None:
        payload = ""
    if not isinstance(payload, str):
        payload = json.dumps(payload)
    if len(payload.encode("utf-8")) > _MAX_PAYLOAD:
        raise ScheduleError("payload is too large")
    enabled_raw = data.get("enabled", True)
    if isinstance(enabled_raw, bool):
        enabled = enabled_raw
    elif isinstance(enabled_raw, int) and enabled_raw in (0, 1):
        enabled = bool(enabled_raw)
    elif isinstance(enabled_raw, str) and enabled_raw.strip().lower() in ("1", "true", "yes", "on"):
        enabled = True
    elif isinstance(enabled_raw, str) and enabled_raw.strip().lower() in ("0", "false", "no", "off"):
        enabled = False
    else:
        raise ScheduleError("enabled must be a boolean")
    return {
        "name": name,
        "cron": cron.expression,
        "timezone": timezone,
        "method": method,
        "path": path,
        "payload": payload,
        "enabled": enabled,
    }


def _schedule_row(row) -> dict:
    return {
        "id": row[0],
        "slug": row[1],
        "name": row[2],
        "cron": row[3],
        "timezone": row[4],
        "method": row[5],
        "path": row[6],
        "payload": row[7],
        "enabled": bool(row[8]),
        "running": bool(row[9]),
        "next_run": row[10],
        "last_run": row[11],
        "last_status": row[12],
        "created_at": row[13],
    }


_COLUMNS = (
    "id, slug, name, cron, timezone, method, path, payload, enabled, running,"
    " next_run, last_run, last_status, created_at"
)


def list_schedules(slug: str, owner_id: int | str = 0) -> list[dict]:
    owner = int(owner_id)
    with db_get(_db_path()) as conn:
        if not _owned(conn, slug, owner):
            raise ScheduleError("not found")
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM schedules WHERE slug = ? ORDER BY name", (slug,)
        ).fetchall()
    return [_schedule_row(row) for row in rows]


def _next_run(expression: str, timezone: str, after: float | None = None) -> float:
    return Cron(expression).next_timestamp(timezone, time.time() if after is None else after)


def create_schedule(slug: str, owner_id: int | str, data: dict) -> dict:
    owner = int(owner_id)
    fields = _validate(data)
    now = time.time()
    next_run = _next_run(fields["cron"], fields["timezone"], now) if fields["enabled"] else None
    with db_get(_db_path()) as conn:
        if not _owned(conn, slug, owner):
            raise ScheduleError("not found")
        count = conn.execute(
            "SELECT COUNT(*) FROM schedules WHERE slug = ?", (slug,)
        ).fetchone()[0]
        if count >= _MAX_SCHEDULES:
            raise ScheduleError(f"schedule quota exceeded (max {_MAX_SCHEDULES})")
        try:
            cur = conn.execute(
                "INSERT INTO schedules (slug, owner_id, name, cron, timezone, method,"
                " path, payload, enabled, next_run, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (slug, owner, fields["name"], fields["cron"], fields["timezone"],
                 fields["method"], fields["path"], fields["payload"],
                 fields["enabled"], next_run, now),
            )
            conn.commit()
        except sqlite3.IntegrityError as error:
            conn.rollback()
            raise ScheduleError("a schedule with that name already exists") from error
    fields.update({"id": cur.lastrowid, "slug": slug, "running": False,
                   "next_run": next_run, "last_run": None, "last_status": None,
                   "created_at": now})
    return fields


def _find(conn, slug: str, schedule_id: int):
    return conn.execute(
        f"SELECT {_COLUMNS} FROM schedules WHERE id = ? AND slug = ?",
        (schedule_id, slug),
    ).fetchone()


def update_schedule(slug: str, owner_id: int | str, schedule_id: int, data: dict) -> dict:
    owner = int(owner_id)
    with db_get(_db_path()) as conn:
        if not _owned(conn, slug, owner):
            raise ScheduleError("not found")
        row = _find(conn, slug, schedule_id)
        if row is None:
            raise ScheduleError("schedule not found")
        current = _schedule_row(row)
        merged = dict(current)
        merged.update({key: value for key, value in data.items() if value is not None})
        fields = _validate(merged)
        next_run = current["next_run"]
        if (
            fields["cron"] != current["cron"]
            or fields["timezone"] != current["timezone"]
            or (fields["enabled"] and not current["enabled"])
        ):
            next_run = _next_run(fields["cron"], fields["timezone"]) if fields["enabled"] else None
        if not fields["enabled"]:
            next_run = None
        try:
            conn.execute(
                "UPDATE schedules SET name=?, cron=?, timezone=?, method=?, path=?,"
                " payload=?, enabled=?, next_run=? WHERE id=? AND slug=?",
                (fields["name"], fields["cron"], fields["timezone"], fields["method"],
                 fields["path"], fields["payload"], fields["enabled"], next_run,
                 schedule_id, slug),
            )
            conn.commit()
        except sqlite3.IntegrityError as error:
            conn.rollback()
            raise ScheduleError("a schedule with that name already exists") from error
    fields.update({"id": schedule_id, "slug": slug, "running": current["running"],
                   "next_run": next_run, "last_run": current["last_run"],
                   "last_status": current["last_status"], "created_at": current["created_at"]})
    return fields


def delete_schedule(slug: str, owner_id: int | str, schedule_id: int) -> bool:
    owner = int(owner_id)
    with db_get(_db_path()) as conn:
        if not _owned(conn, slug, owner):
            raise ScheduleError("not found")
        cur = conn.execute("DELETE FROM schedules WHERE id = ? AND slug = ?", (schedule_id, slug))
        conn.execute("DELETE FROM schedule_runs WHERE schedule_id = ?", (schedule_id,))
        conn.commit()
        return cur.rowcount > 0


def list_runs(slug: str, owner_id: int | str, schedule_id: int, limit: int = 100) -> list[dict]:
    owner = int(owner_id)
    with db_get(_db_path()) as conn:
        if not _owned(conn, slug, owner):
            raise ScheduleError("not found")
        rows = conn.execute(
            "SELECT id, request_id, started, finished, status, error"
            " FROM schedule_runs WHERE schedule_id = ? AND slug = ?"
            " ORDER BY id DESC LIMIT ?",
            (schedule_id, slug, max(1, min(int(limit), 500))),
        ).fetchall()
    return [
        {"id": r[0], "request_id": r[1], "started": r[2], "finished": r[3],
         "status": r[4], "error": r[5]}
        for r in rows
    ]


def _instance_id() -> str:
    base = os.environ.get("VLAMBDA_INSTANCE_ID") or socket.gethostname()
    return f"{base}:{os.getpid()}"


def running() -> bool:
    return _worker is not None


def is_leader() -> bool:
    now = time.time()
    try:
        with db_get(_db_path()) as conn:
            row = conn.execute(
                "SELECT owner, expires FROM scheduler_lease WHERE id = 1"
            ).fetchone()
    except Exception:
        return False
    return bool(row and row[1] > now and row[0] == _instance_id())


def _acquire_lease(conn, owner: str, now: float) -> bool:
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT owner, expires FROM scheduler_lease WHERE id = 1").fetchone()
    if row is None or row[1] <= now or row[0] == owner:
        conn.execute(
            "INSERT INTO scheduler_lease (id, owner, expires) VALUES (1, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET owner=excluded.owner, expires=excluded.expires",
            (owner, now + _LEASE_TTL),
        )
        conn.commit()
        return True
    conn.commit()
    return False


class Scheduler:
    def __init__(self, executor):
        self.executor = executor
        self.instance = _instance_id()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="vlambda-schedule")
        self._last_cleanup = 0.0

    def start(self):
        ensure_schema()
        self._thread = threading.Thread(target=self._loop, name="vlambda-scheduler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._pool.shutdown(wait=False)

    def _loop(self):
        tick = float(os.environ.get("VLAMBDA_SCHEDULE_TICK", "1.0"))
        while not self._stop.wait(max(0.2, tick)):
            try:
                self._tick()
            except Exception as error:
                print(f"scheduler tick failed: {type(error).__name__}: {error}", flush=True)

    def _tick(self):
        now = time.time()
        claimed = []
        with db_get(_db_path()) as conn:
            if not _acquire_lease(conn, self.instance, now):
                return
            conn.execute("BEGIN IMMEDIATE")
            # A killed worker must not keep a schedule locked forever.
            stale = now - max(120, float(os.environ.get("VLAMBDA_TIMEOUT", "30")) * 2)
            conn.execute(
                "UPDATE schedule_runs SET status='failed', error='timeout', finished=?"
                " WHERE status='running' AND schedule_id IN ("
                " SELECT id FROM schedules WHERE running=1"
                " AND run_started IS NOT NULL AND run_started < ?)",
                (now, stale),
            )
            conn.execute(
                "UPDATE schedules SET running=0, last_status='timeout'"
                " WHERE running=1 AND run_started IS NOT NULL AND run_started < ?",
                (stale,),
            )
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM schedules"
                " WHERE enabled=1 AND running=0 AND next_run IS NOT NULL AND next_run <= ?"
                " ORDER BY next_run LIMIT 10",
                (now,),
            ).fetchall()
            for row in rows:
                cur = conn.execute(
                    "UPDATE schedules SET running=1, run_started=?"
                    " WHERE id=? AND running=0 AND next_run=?",
                    (now, row[0], row[10]),
                )
                if cur.rowcount == 1:
                    claimed.append(_schedule_row(row))
            conn.commit()
        for schedule in claimed:
            self._pool.submit(self._run_claimed, schedule, now)
        if now - self._last_cleanup > 300:
            self._last_cleanup = now
            self._cleanup(now)

    def _run_claimed(self, schedule: dict, claimed_at: float):
        missed = claimed_at - float(schedule["next_run"] or claimed_at) > _GRACE
        request_id = f"sched-{schedule['id']}-{int(claimed_at)}"
        with db_get(_db_path()) as conn:
            cur = conn.execute(
                "INSERT INTO schedule_runs (schedule_id, slug, request_id, started, status)"
                " VALUES (?, ?, ?, ?, ?)",
                (schedule["id"], schedule["slug"], request_id, claimed_at,
                 "missed" if missed else "running"),
            )
            run_id = cur.lastrowid
            conn.commit()
        if missed:
            self._finish(schedule, run_id, claimed_at, "missed", "missed run window", None)
            return
        started = time.time()
        error = ""
        try:
            result = self.executor(
                schedule["slug"], schedule["method"], schedule["path"],
                schedule["payload"], request_id, schedule["name"],
            )
            if isinstance(result, dict) and result.get("error"):
                error = str(result["error"])
            elif isinstance(result, dict) and int(result.get("status", 200)) >= 500:
                error = f"HTTP {result.get('status')}"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        elapsed = (time.time() - started) * 1000.0
        status = "failed" if error else "completed"
        try:
            from vlambda.metrics import record

            record(request_id, schedule["slug"], schedule["method"], schedule["path"], elapsed, error)
        except Exception:
            pass
        self._finish(schedule, run_id, started, status, error, elapsed)

    def _finish(self, schedule: dict, run_id: int, started: float, status: str,
                error: str, elapsed: float | None):
        now = time.time()
        next_run = None
        if schedule["enabled"]:
            try:
                next_run = _next_run(schedule["cron"], schedule["timezone"], now)
            except Exception:
                next_run = None
        with db_get(_db_path()) as conn:
            conn.execute(
                "UPDATE schedule_runs SET finished=?, status=?, error=? WHERE id=?",
                (now, status, error or None, run_id),
            )
            conn.execute(
                "UPDATE schedules SET running=0, run_started=NULL, last_run=?,"
                " last_status=?, next_run=? WHERE id=?",
                (now, status, next_run, schedule["id"]),
            )
            conn.commit()

    def _cleanup(self, now: float):
        cutoff = now - max(1, _RETENTION_DAYS) * 86400
        with db_get(_db_path()) as conn:
            conn.execute("DELETE FROM schedule_runs WHERE started < ?", (cutoff,))
            conn.execute(
                "DELETE FROM schedule_runs WHERE id NOT IN ("
                " SELECT id FROM schedule_runs r2 WHERE r2.schedule_id = schedule_runs.schedule_id"
                " ORDER BY id DESC LIMIT 1000)"
            )
            conn.commit()

    def run_now(self, schedule: dict) -> int:
        now = time.time()
        request_id = f"sched-{schedule['id']}-{int(now)}"
        with db_get(_db_path()) as conn:
            cur = conn.execute(
                "UPDATE schedules SET running=1, run_started=? WHERE id=? AND running=0",
                (now, schedule["id"]),
            )
            if cur.rowcount != 1:
                conn.rollback()
                raise ScheduleError("schedule is already running")
            run = conn.execute(
                "INSERT INTO schedule_runs (schedule_id, slug, request_id, started, status)"
                " VALUES (?, ?, ?, ?, 'running')",
                (schedule["id"], schedule["slug"], request_id, now),
            )
            run_id = run.lastrowid
            conn.commit()
        self._pool.submit(self._run_manual, schedule, run_id, request_id, now)
        return run_id

    def _run_manual(self, schedule: dict, run_id: int, request_id: str, started: float):
        error = ""
        try:
            result = self.executor(
                schedule["slug"], schedule["method"], schedule["path"],
                schedule["payload"], request_id, schedule["name"],
            )
            if isinstance(result, dict) and result.get("error"):
                error = str(result["error"])
            elif isinstance(result, dict) and int(result.get("status", 200)) >= 500:
                error = f"HTTP {result.get('status')}"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        elapsed = (time.time() - started) * 1000.0
        status = "failed" if error else "completed"
        try:
            from vlambda.metrics import record

            record(request_id, schedule["slug"], schedule["method"], schedule["path"], elapsed, error)
        except Exception:
            pass
        self._finish(schedule, run_id, started, status, error, elapsed)


def start(executor) -> bool:
    global _worker
    if _worker is not None:
        return True
    try:
        ensure_schema()
        _worker = Scheduler(executor)
        _worker.start()
        return True
    except Exception as error:
        print(f"WARNING: scheduler disabled: {type(error).__name__}: {error}", flush=True)
        _worker = None
        return False


def stop() -> None:
    global _worker
    worker, _worker = _worker, None
    if worker is not None:
        worker.stop()


def run_now(slug: str, owner_id: int | str, schedule_id: int) -> dict:
    owner = int(owner_id)
    with db_get(_db_path()) as conn:
        if not _owned(conn, slug, owner):
            raise ScheduleError("not found")
        row = _find(conn, slug, schedule_id)
        if row is None:
            raise ScheduleError("schedule not found")
        schedule = _schedule_row(row)
    if _worker is None:
        raise ScheduleError("scheduler is not running")
    run_id = _worker.run_now(schedule)
    return {"run_id": run_id, "status": "pending"}


def handle_api(slug: str, owner_id: str, action: str, method: str,
               schedule_id: str, body: str) -> str:
    try:
        owner = int(owner_id)
        ensure_schema()
        if action == "runs":
            if method != "GET":
                return json.dumps({"error": "method not allowed"})
            return json.dumps({
                "runs": list_runs(slug, owner, int(schedule_id)),
            })
        if action == "run":
            if method != "POST":
                return json.dumps({"error": "method not allowed"})
            return json.dumps(run_now(slug, owner, int(schedule_id)))
        if schedule_id:
            sid = int(schedule_id)
            if method == "PUT":
                try:
                    data = json.loads(body or "{}")
                except Exception as error:
                    return json.dumps({"error": f"invalid json: {error}"})
                if not isinstance(data, dict):
                    return json.dumps({"error": "body must be an object"})
                return json.dumps({"schedule": update_schedule(slug, owner, sid, data)})
            if method == "DELETE":
                return json.dumps({"deleted": delete_schedule(slug, owner, sid)})
            return json.dumps({"error": "method not allowed"})
        if method == "GET":
            return json.dumps({"schedules": list_schedules(slug, owner)})
        if method == "POST":
            try:
                data = json.loads(body or "{}")
            except Exception as error:
                return json.dumps({"error": f"invalid json: {error}"})
            if not isinstance(data, dict):
                return json.dumps({"error": "body must be an object"})
            return json.dumps({"schedule": create_schedule(slug, owner, data)})
        return json.dumps({"error": "method not allowed"})
    except (ScheduleError, CronError) as error:
        return json.dumps({"error": str(error)})
    except Exception as error:
        return json.dumps({"error": f"{type(error).__name__}: {error}"})
