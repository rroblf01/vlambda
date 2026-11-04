#!/usr/bin/env python3
"""Run the single-host chaos checklist against disposable servers.

Scenarios: SQLite lock recovery, capability broker loss, read-only startup
failure, and backup/restore round-trip. The script starts its own servers, so
it never touches production data.

    python scripts/chaos.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.environ.get("VLAMBDA_PYTHON", sys.executable)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _req(base: str, method: str, path: str, data=None, token: str = "", timeout: float = 20):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = data.encode() if isinstance(data, str) else data
    request = urllib.request.Request(base + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            try:
                return response.status, json.loads(raw) if raw else {}
            except Exception:
                return response.status, raw
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw) if raw else {}
        except Exception:
            return error.code, raw


def _sandbox_mode() -> str:
    try:
        import ctypes.util

        from vlambda.sandbox_os import sandbox_supported
        if sandbox_supported() and ctypes.util.find_library("seccomp"):
            return "fork"
    except Exception:
        pass
    return "process"


class SkipScenario(RuntimeError):
    pass


class Server:
    def __init__(self, workdir: str, extra_env=None, port: int | None = None):
        self.dir = workdir
        self.port = port or _free_port()
        self.db = os.path.join(workdir, "vlambda.db")
        env = os.environ.copy()
        env.update({
            "VLAMBDA_DB": self.db,
            "VLAMBDA_METRICS_DB": os.path.join(workdir, "metrics.db"),
            "VLAMBDA_PROJECTS_DIR": os.path.join(workdir, "projects"),
            "VLAMBDA_PACKAGES_DIR": os.path.join(workdir, "packages"),
            "VLAMBDA_CACHE_DIR": os.path.join(workdir, "cache"),
            "VLAMBDA_BROKER_SOCKET": os.path.join(workdir, "broker.sock"),
            "VLAMBDA_SECRET": "chaos-secret",
            "VLAMBDA_ADMIN_TOKEN": "chaos-admin",
            "VLAMBDA_SANDBOX": _sandbox_mode(),
            "VLAMBDA_SECCOMP_MODE": "allowlist",
            "VLAMBDA_TIMEOUT": "10",
        })
        env.update(extra_env or {})
        self.log = open(os.path.join(workdir, "server.log"), "w")
        self.proc = subprocess.Popen(
            [PYTHON, "-m", "vlambda", "--port", str(self.port), "--sqlite_path", self.db],
            cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT,
        )
        self.base = f"http://127.0.0.1:{self.port}"

    def wait(self, timeout: float = 30) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("server exited:\n" + self.tail())
            try:
                if _req(self.base, "GET", "/health", timeout=2)[0] == 200:
                    return
            except Exception:
                pass
            time.sleep(0.3)
        raise RuntimeError("server did not start:\n" + self.tail())

    def login(self) -> str:
        status, body = _req(self.base, "POST", "/register", "chaos:chaos-passphrase-123")
        assert status == 201, (status, body)
        status, body = _req(self.base, "POST", "/login", "chaos:chaos-passphrase-123")
        assert status == 200, (status, body)
        return body["token"]

    def tail(self, limit: int = 3000) -> str:
        try:
            with open(os.path.join(self.dir, "server.log"), errors="replace") as handle:
                return handle.read()[-limit:]
        except OSError:
            return ""

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.log.close()


def _scenario_db_lock(workdir: str) -> None:
    server = Server(workdir, {"VLAMBDA_RATE_LIMIT": "100000"})
    try:
        server.wait()
        token = server.login()
        lock = sqlite3.connect(server.db, timeout=5)
        lock.execute("BEGIN EXCLUSIVE")
        result = {}

        def management():
            result["response"] = _req(server.base, "GET", "/vlambda?format=json", token=token, timeout=25)

        thread = threading.Thread(target=management)
        thread.start()
        time.sleep(1.5)
        health = _req(server.base, "GET", "/health", timeout=3)
        assert health[0] == 200, health
        lock.execute("COMMIT")
        lock.close()
        thread.join(timeout=25)
        assert not thread.is_alive(), "management request did not recover after the lock"
        status, body = result["response"]
        assert status == 200 and "lambdas" in body, (status, body)
    finally:
        server.stop()


def _scenario_broker_loss(workdir: str) -> None:
    server = Server(workdir, {"VLAMBDA_RATE_LIMIT": "100000"})
    try:
        server.wait()
        token = server.login()
        status, body = _req(
            server.base, "POST", "/vlambda?name=kv", token=token,
            data="def handler(r):\n    r.kv.set('k', 'v')\n    return {'ok': True}\n",
        )
        assert status == 201, (status, body)
        slug = body["slug"]
        status, body = _req(server.base, "GET", f"/f/{slug}", token=token)
        assert status == 200, (status, body)

        os.unlink(os.path.join(workdir, "broker.sock"))
        status, body = _req(server.base, "GET", f"/f/{slug}", token=token, timeout=15)
        assert status >= 400, (status, body)

        status, body = _req(
            server.base, "POST", "/vlambda?name=plain", token=token,
            data="def handler(r): return {'ok': True}\n",
        )
        assert status == 201, (status, body)
        status, body = _req(server.base, "GET", f"/f/{body['slug']}", token=token)
        assert status == 200 and body == {"ok": True}, (status, body)
    finally:
        server.stop()


def _scenario_readonly_startup(workdir: str) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise SkipScenario("root can write to a 0500 directory")
    projects = os.path.join(workdir, "readonly-projects")
    os.makedirs(projects, exist_ok=True)
    os.chmod(projects, 0o500)
    ro_dir = os.path.join(workdir, "ro")
    os.makedirs(ro_dir, exist_ok=True)
    server = Server(ro_dir, {
        "VLAMBDA_PROJECTS_DIR": projects,
        "VLAMBDA_SANDBOX": _sandbox_mode(),
    })
    try:
        try:
            server.wait(timeout=15)
            raise AssertionError("server started with a read-only projects directory")
        except RuntimeError:
            pass
    finally:
        server.stop()
        os.chmod(projects, 0o700)


def _scenario_backup_restore(workdir: str) -> None:
    server = Server(workdir, {"VLAMBDA_RATE_LIMIT": "100000"})
    try:
        server.wait()
        token = server.login()
        status, body = _req(
            server.base, "POST", "/vlambda?name=backup", token=token,
            data="def handler(r): return {'ok': True}\n",
        )
        assert status == 201, (status, body)
    finally:
        server.stop()

    from vlambda import backup

    archive = os.path.join(workdir, "backup.tar.gz")
    manifest = backup.create_backup(
        archive, db_path=server.db, projects_dir=os.path.join(workdir, "projects"), encrypt=True,
    )
    assert manifest["schema_version"] == 1, manifest
    restored = os.path.join(workdir, "restored")
    os.makedirs(restored, exist_ok=True)
    result = backup.restore_backup(
        archive, db_path=os.path.join(restored, "vlambda.db"),
        projects_dir=os.path.join(restored, "projects"),
    )
    assert result["restored"] and result["encrypted"], result
    verify = Server(restored)
    try:
        verify.wait()
        assert _req(verify.base, "GET", "/health")[0] == 200
    finally:
        verify.stop()


def main() -> int:
    scenarios = (
        ("db-lock", _scenario_db_lock),
        ("broker-loss", _scenario_broker_loss),
        ("readonly-startup", _scenario_readonly_startup),
        ("backup-restore", _scenario_backup_restore),
    )
    failed = 0
    for name, func in scenarios:
        workdir = tempfile.mkdtemp(prefix=f"vlambda-chaos-{name}-")
        try:
            func(workdir)
            print(f"PASS {name}")
        except SkipScenario as exc:
            print(f"SKIP {name}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
