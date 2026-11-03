"""Pre-warmed fork supervisor for the vlambda sandbox.

The supervisor imports the Python runtime once, then forks a fresh child per
invocation. Each child applies Landlock + seccomp before executing user code and
exits afterwards, so requests never share mutable state.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import sys
import tempfile
import time

from vlambda.pool import _landlock_read_paths
from vlambda.runner import handle_payload
from vlambda.sandbox_os import _apply_landlock


def _write_envelope(fd: int, envelope: dict):
    data = (json.dumps(envelope) + "\n").encode("utf-8")
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _child(payload: dict, read_fd: int, write_fd: int, read_paths: list[str], tmp_dir: str, cpu_sec: int):
    try:
        os.close(read_fd)
    except Exception:
        pass
    devnull = os.open(os.devnull, os.O_RDWR)
    try:
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
    finally:
        if devnull > 2:
            os.close(devnull)
    os.environ["HOME"] = tmp_dir
    os.environ["TMPDIR"] = tmp_dir
    try:
        from vlambda.sandbox_os import apply_sandbox
        apply_sandbox(read_paths, [tmp_dir], cpu_sec)
        envelope = handle_payload(payload)
    except Exception as e:
        envelope = {"ok": False, "error": f"sandbox: {type(e).__name__}: {e}", "logs": []}
    try:
        _write_envelope(write_fd, envelope)
    finally:
        try:
            os.close(write_fd)
        except OSError:
            pass
        os._exit(0)


def _run_one(payload: dict) -> dict:
    timeout = int(payload.get("timeout", 0) or 5)
    project_dir = payload.get("project_dir", "")
    vendor_dir = payload.get("vendor_dir", "")
    tmp_dir = tempfile.mkdtemp(prefix="vlambda-fork-", dir="/tmp")
    read_paths = list(BASE_READ_PATHS)
    if project_dir:
        read_paths.append(project_dir)
    if vendor_dir:
        read_paths.append(vendor_dir)
    try:
        meta = json.loads(payload.get("request_meta", "{}") or "{}")
        broker_socket = meta.get("broker_socket", "") if isinstance(meta, dict) else ""
        if broker_socket:
            broker_dir = os.path.dirname(broker_socket)
            if broker_dir and broker_dir not in read_paths:
                read_paths.append(broker_dir)
    except Exception:
        pass
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        _child(payload, read_fd, write_fd, read_paths, tmp_dir, timeout)
        os._exit(1)
    os.close(write_fd)
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout + 2
    timed_out = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([read_fd], [], [], remaining)
            if not ready:
                timed_out = True
                break
            data = os.read(read_fd, 65536)
            if not data:
                break
            chunks.append(data)
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass
        if timed_out:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)
    if timed_out:
        return {"ok": False, "error": "invocation timeout", "logs": []}
    raw = b"".join(chunks)
    if not raw:
        return {"ok": False, "error": "sandbox exited", "logs": []}
    try:
        return json.loads(raw.decode("utf-8", "replace").splitlines()[-1])
    except Exception:
        return {"ok": False, "error": "invalid runner response", "logs": []}


# The supervisor itself is trusted and never executes user code, but keeping it
# away from the database gives the fork child an extra boundary.
BASE_READ_PATHS = _landlock_read_paths()
for extra in ("/data/projects", "/data/packages"):
    if os.path.isdir(extra):
        BASE_READ_PATHS.append(extra)
# Only the supervisor needs this path; do not leak it into user code.
os.environ.pop("VLRUN_BROKER_DIR", None)
try:
    _apply_landlock(BASE_READ_PATHS, ["/tmp"])
except Exception as e:
    print(f"forkd: parent sandbox failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}", "logs": []}), flush=True)
            continue
        envelope = _run_one(payload)
        print(json.dumps(envelope), flush=True)


if __name__ == "__main__":
    main()
