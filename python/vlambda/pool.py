"""Python worker pool for executing lambdas.

Each worker runs in its own thread with its own sandbox namespace.
Code is compiled once and cached. V dispatches requests to available workers.
"""

from __future__ import annotations

import threading
import time
import queue
import itertools
import os
import sys
import json
import base64
import select
import subprocess
import shutil
import tempfile
from typing import Any

from vlambda.sandbox import execute_lambda, make_sandbox
from vlambda.async_support import resolve
from vlambda.capabilities import CapabilityClient, clear_current, set_current
from vlambda.request import Request
from vlambda.cache import get as get_compiled


class Worker:
    def __init__(self, worker_id: int):
        self.id = worker_id
        self._queue: queue.Queue = queue.Queue(maxsize=int(os.environ.get("VLAMBDA_QUEUE_SIZE", "1000")))
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            task = self._queue.get()
            if task is None:
                break
            result_queue = task["result_queue"]
            client = None
            try:
                meta = json.loads(task.get("request_meta", "{}") or "{}")
                broker_socket = meta.get("broker_socket", "")
                broker_token = meta.get("broker_token", "")
                if broker_socket and broker_token:
                    client = CapabilityClient(str(broker_socket), str(broker_token))
                set_current(client)
                req_obj = Request(
                    method=task.get("method", "GET"),
                    path=task.get("path", "/"),
                    query_string=task.get("query_string", ""),
                    headers=meta.get("headers", {}),
                    body=task.get("body", b""),
                    request_id=task.get("request_id", ""),
                    slug=task.get("slug", ""),
                    remote_ip=meta.get("remote_ip", ""),
                    host=meta.get("host", ""),
                    scheme=meta.get("scheme", "http"),
                    env=meta.get("env", {}),
                    kv_client=client,
                    fetch_client=client,
                    trigger_client=client,
                )
                if task.get("kind") == "project":
                    from vlambda.project import execute_project
                    result = execute_project(task["project_dir"], task["entrypoint"], req_obj)
                else:
                    vendor_dir = task.get("vendor_dir", "")
                    if vendor_dir and vendor_dir not in sys.path:
                        sys.path.insert(0, vendor_dir)
                    compiled, ns_template, handler = get_compiled(task["code"], task.get("code_key") or None)
                    if handler is None:
                        raise ValueError("no handler function defined")
                    ns = dict(ns_template)
                    ns["request"] = req_obj
                    result = resolve(handler(req_obj))
                from vlambda.response import serialize
                result = serialize(result)
            except Exception as e:
                result = {"error": f"{type(e).__name__}: {e}"}
            finally:
                clear_current()
            result_queue.put(result)

    # Single-flight: coalesce identical in-flight requests
    _inflight: dict = {}
    _inflight_lock = threading.Lock()

    _SINGLE_FLIGHT = __import__("os").environ.get("VLAMBDA_SINGLE_FLIGHT", "0") == "1"

    def submit(self, code, method, path, query, body, timeout=0, memory_mb=0, vendor_dir="", request_id="", slug="", request_meta="", code_key="") -> Any:
        if memory_mb and memory_mb > 0:
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
            if usage > memory_mb * 1024 * 1024:
                return {"error": "memory quota exceeded"}
        if not Worker._SINGLE_FLIGHT:
            result_queue = queue.Queue(1)
            task = {
                "kind": "code", "code": code, "method": method, "path": path,
                "query_string": query, "body": body, "vendor_dir": vendor_dir,
                "request_id": request_id, "slug": slug, "request_meta": request_meta,
                "code_key": code_key or "",
                "result_queue": result_queue,
            }
            try:
                self._queue.put(task, timeout=5)
            except queue.Full:
                return {"error": "server busy, try again later"}
            try:
                _t = timeout if timeout and timeout > 0 else int(os.environ.get("VLAMBDA_INVOKE_TIMEOUT", "30"))
                return result_queue.get(timeout=_t)
            except queue.Empty:
                return {"error": "invocation timeout"}

        import hashlib
        key = hashlib.sha256(f"{code}:{method}:{path}:{query}:{body}".encode()).hexdigest()
        with Worker._inflight_lock:
            if key in Worker._inflight:
                # Wait for the in-flight result
                event, container = Worker._inflight[key]
                is_leader = False
            else:
                event = threading.Event()
                container = {"result": None}
                Worker._inflight[key] = (event, container)
                is_leader = True

        if not is_leader:
            event.wait(timeout=timeout if timeout and timeout > 0 else int(os.environ.get("VLAMBDA_INVOKE_TIMEOUT", "30")))
            return container.get("result", {"error": "timeout"})

        # Leader executes
        result_queue = queue.Queue(1)
        task = {
            "kind": "code", "code": code, "method": method, "path": path,
            "query_string": query, "body": body, "vendor_dir": vendor_dir,
            "request_id": request_id, "slug": slug, "request_meta": request_meta,
            "code_key": code_key or "",
            "result_queue": result_queue,
        }
        try:
            self._queue.put(task, timeout=5)
        except queue.Full:
            with Worker._inflight_lock:
                Worker._inflight.pop(key, None)
            event.set()
            return {"error": "server busy, try again later"}
        try:
            _t = timeout if timeout and timeout > 0 else int(os.environ.get("VLAMBDA_INVOKE_TIMEOUT", "30"))
            result = result_queue.get(timeout=_t)
        except queue.Empty:
            result = {"error": "invocation timeout"}
        container["result"] = result
        with Worker._inflight_lock:
            Worker._inflight.pop(key, None)
        event.set()
        return result

    def submit_project(self, project_dir, entrypoint, method, path, query, body, timeout=0, request_id="", slug="", request_meta="") -> Any:
        result_queue = queue.Queue(1)
        task = {
            "kind": "project", "project_dir": project_dir, "entrypoint": entrypoint,
            "method": method, "path": path, "query_string": query, "body": body,
            "request_id": request_id, "slug": slug, "request_meta": request_meta,
            "result_queue": result_queue,
        }
        try:
            self._queue.put(task, timeout=5)
        except queue.Full:
            return {"error": "server busy, try again later"}
        try:
            _t = timeout if timeout and timeout > 0 else int(os.environ.get("VLAMBDA_INVOKE_TIMEOUT", "30"))
            return result_queue.get(timeout=_t)
        except queue.Empty:
            return {"error": "invocation timeout"}

    def stop(self):
        self._queue.put(None)


class InProcessPool:
    def __init__(self, size: int):
        self._max_workers = max(size, int(os.environ.get("VLAMBDA_MAX_WORKERS", "64")))
        self._workers = [Worker(i) for i in range(size)]
        self._lock = threading.Lock()
        self._counter = itertools.count()

    def execute(self, code, method, path, query, body, timeout=0, memory_mb=0, vendor_dir="", request_id="", slug="", request_meta="", code_key="") -> Any:
        # Auto-scale: if queue is deep, add a worker up to the configured cap.
        with self._lock:
            if len(self._workers) < self._max_workers and self._queue_depth() > len(self._workers) * 2:
                self._workers.append(Worker(len(self._workers)))
            workers = list(self._workers)
        # Atomic round-robin selection without holding the lock during submit.
        idx = next(self._counter) % len(workers)
        return workers[idx].submit(code, method, path, query, body, timeout, memory_mb, vendor_dir, request_id, slug, request_meta, code_key)

    def execute_project(self, project_dir, entrypoint, method, path, query, body, timeout=0, request_id="", slug="", request_meta=""):
        with self._lock:
            workers = list(self._workers)
        idx = next(self._counter) % len(workers)
        return workers[idx].submit_project(project_dir, entrypoint, method, path, query, body, timeout, request_id, slug, request_meta)

    def _queue_depth(self) -> int:
        return sum(w._queue.qsize() for w in self._workers)

    def stats(self):
        return {
            "workers": len(self._workers),
            "max_workers": self._max_workers,
            "queue_depth": self._queue_depth(),
        }

    def stop(self):
        for w in self._workers:
            w.stop()



def _runner_command():
    """Return the command used to start a persistent runner process."""
    return [sys.executable, "-m", "vlambda.runner"]


_NSJAIL_SECCOMP = (
    "POLICY vlambda { DENY { mount, umount, pivot_root, chroot, setns, unshare, "
    "ptrace, process_vm_readv, process_vm_writev, bpf, perf_event_open, "
    "keyctl, add_key, request_key, kexec_load, kexec_file_load, reboot, "
    "swapon, swapoff, init_module, finit_module, delete_module, "
    "open_by_handle_at, name_to_handle_at, userfaultfd, io_uring_setup, "
    "io_uring_enter, io_uring_register, acct, quotactl, syslog, adjtimex, "
    "clock_settime, settimeofday, sethostname, setdomainname, vhangup, "
    "nfsservctl, open_tree, move_mount, fsopen, fsconfig, fsmount, fspick, "
    "mount_setattr, setuid, setgid, setreuid, setregid, setresuid, setresgid, "
    "setgroups, capset } } USE vlambda DEFAULT ALLOW"
)


def _nsjail_config(project_dir: str = "", vendor_dir: str = "", broker_dir: str = "") -> str:
    mounts = [
        'mount { src: "/" dst: "/" is_bind: true rw: false }',
        'mount { src: "/opt/python" dst: "/opt/python" is_bind: true rw: false }',
        'mount { src: "/opt/venv" dst: "/opt/venv" is_bind: true rw: false }',
    ]
    if project_dir:
        mounts.append(f'mount {{ src: "{project_dir}" dst: "/work" is_bind: true rw: false }}')
    if vendor_dir:
        mounts.append(f'mount {{ src: "{vendor_dir}" dst: "/vendor" is_bind: true rw: false }}')
    if broker_dir:
        mounts.append(f'mount {{ src: "{broker_dir}" dst: "/broker" is_bind: true rw: false }}')
    # Hide the server's data/secret-bearing paths after binding what the runner needs.
    for hidden in ("/data", "/home", "/root", "/run", "/sys", "/proc"):
        mounts.append(f'mount {{ src: "/opt/empty" dst: "{hidden}" is_bind: true rw: false }}')
    mounts.extend([
        'mount { dst: "/tmp" fstype: "tmpfs" rw: true }',
        'mount { dst: "/dev/shm" fstype: "tmpfs" rw: true }',
    ])
    return f"""name: "vlambda"
mode: ONCE
hostname: "vlambda"
cwd: "/tmp"
clone_newnet: true
clone_newuser: true
clone_newns: true
clone_newpid: true
clone_newipc: true
clone_newuts: true
clone_newcgroup: false
iface_no_lo: true
mount_proc: false
keep_caps: false
rlimit_as: {os.environ.get("VLAMBDA_NSJAIL_AS_MB", "256")}
rlimit_cpu: {os.environ.get("VLAMBDA_NSJAIL_CPU", "5")}
rlimit_nofile: 64
rlimit_nproc: 16
rlimit_fsize: 16
time_limit: {os.environ.get("VLAMBDA_NSJAIL_TIME", "5")}
max_cpus: 1
uidmap {{
  inside_id: "65534"
  outside_id: ""
  count: 1
}}
gidmap {{
  inside_id: "65534"
  outside_id: ""
  count: 1
}}
{"\n".join(mounts)}
seccomp_string: "{_NSJAIL_SECCOMP}"
exec_bin {{
  path: "/opt/venv/bin/python"
  arg: "-m"
  arg: "vlambda.runner"
}}
"""


_RUNNER_ENV_ALLOW = {
    "PATH", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TZ", "TERM",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "LD_LIBRARY_PATH",
    "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONHASHSEED",
}


def _runner_env() -> dict[str, str]:
    """Minimal environment for lambda runners; server secrets must not leak."""
    env = {key: value for key, value in os.environ.items() if key in _RUNNER_ENV_ALLOW}
    env["HOME"] = "/tmp"
    env["TMPDIR"] = "/tmp"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["VLRUN_NO_NATIVE"] = "1"
    return env


def _record_runner_logs(logs) -> None:
    """Persist logs produced in a short-lived runner process."""
    if not isinstance(logs, list) or not logs:
        return
    try:
        from vlambda.metrics import record_log
    except Exception:
        return
    for item in logs:
        if not isinstance(item, dict):
            continue
        try:
            record_log(
                str(item.get("request_id", "")),
                str(item.get("slug", "")),
                str(item.get("level", "info")),
                str(item.get("message", "")),
            )
        except Exception:
            pass


class ProcessWorker:
    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._start()

    def _start(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = _runner_env()
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        self._proc = subprocess.Popen(
            _runner_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            cwd=root,
            env=env,
        )

    def submit(self, code, method, path, query, body, timeout=0, memory_mb=0, vendor_dir="", request_id="", slug="", request_meta="", code_key=""):
        payload = {
            "kind": "code",
            "code": code,
            "method": method,
            "path": path,
            "query_string": query,
            "body_b64": base64.b64encode(body or b"").decode("ascii"),
            "vendor_dir": vendor_dir or "",
            "request_id": request_id or "",
            "slug": slug or "",
            "request_meta": request_meta or "",
            "code_key": code_key or "",
        }
        return self._call(payload, timeout)

    def submit_project(self, project_dir, entrypoint, method, path, query, body, timeout=0, request_id="", slug="", request_meta=""):
        payload = {
            "kind": "project",
            "project_dir": project_dir,
            "entrypoint": entrypoint,
            "method": method,
            "path": path,
            "query_string": query,
            "body_b64": base64.b64encode(body or b"").decode("ascii"),
            "request_id": request_id or "",
            "slug": slug or "",
            "request_meta": request_meta or "",
        }
        return self._call(payload, timeout)

    def _call(self, payload: dict, timeout: int = 0):
        timeout = timeout if timeout and timeout > 0 else int(os.environ.get("VLAMBDA_INVOKE_TIMEOUT", "30"))
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()
            try:
                self._proc.stdin.write(json.dumps(payload) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._start()
                return {"error": "runner unavailable"}
            ready, _, _ = select.select([self._proc.stdout], [], [], timeout)
            if not ready:
                try:
                    self._proc.kill()
                except Exception:
                    pass
                self._proc = None
                return {"error": "invocation timeout"}
            line = self._proc.stdout.readline()
            if not line:
                self._proc = None
                return {"error": "runner exited"}
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError:
                self._proc = None
                return {"error": "invalid runner response"}
            _record_runner_logs(envelope.get("logs"))
            result = envelope.get("response") if envelope.get("ok") else {"error": envelope.get("error", "execution failed")}
            # A shared runner would leak modules between projects/lambdas, so
            # the process mode starts a fresh interpreter for every request.
            self.stop()
            return result

    def stop(self):
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass


class ProcessPool:
    def __init__(self, size: int):
        self._workers = [ProcessWorker() for _ in range(max(1, size))]
        self._counter = itertools.count()
        self._lock = threading.Lock()
        self._max_workers = len(self._workers)

    def execute(self, code, method, path, query, body, timeout=0, memory_mb=0, vendor_dir="", request_id="", slug="", request_meta="", code_key=""):
        with self._lock:
            workers = list(self._workers)
        idx = next(self._counter) % len(workers)
        return workers[idx].submit(code, method, path, query, body, timeout, memory_mb, vendor_dir, request_id, slug, request_meta, code_key)

    def execute_project(self, project_dir, entrypoint, method, path, query, body, timeout=0, request_id="", slug="", request_meta=""):
        with self._lock:
            workers = list(self._workers)
        idx = next(self._counter) % len(workers)
        return workers[idx].submit_project(project_dir, entrypoint, method, path, query, body, timeout, request_id, slug, request_meta)

    def _queue_depth(self) -> int:
        return 0

    def stats(self):
        return {"workers": len(self._workers), "max_workers": self._max_workers, "queue_depth": 0, "mode": "process"}

    def stop(self):
        for worker in self._workers:
            worker.stop()


class ForkWorker:
    """A pre-warmed supervisor that forks one sandboxed child per request."""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._start()

    def _start(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = _runner_env()
        env["HOME"] = "/tmp"
        env["TMPDIR"] = "/tmp"
        broker_socket = os.environ.get("VLAMBDA_BROKER_SOCKET", "")
        if broker_socket:
            env["VLRUN_BROKER_DIR"] = os.path.dirname(broker_socket)
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "vlambda.forkd"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            cwd=root,
            env=env,
        )

    def submit(self, code, method, path, query, body, timeout=0, memory_mb=0, vendor_dir="", request_id="", slug="", request_meta="", code_key=""):
        timeout = timeout if timeout and timeout > 0 else int(os.environ.get("VLAMBDA_INVOKE_TIMEOUT", "30"))
        payload = {
            "kind": "code",
            "code": code,
            "method": method,
            "path": path,
            "query_string": query,
            "body_b64": base64.b64encode(body or b"").decode("ascii"),
            "vendor_dir": vendor_dir or "",
            "request_id": request_id or "",
            "slug": slug or "",
            "request_meta": request_meta or "",
            "code_key": code_key or "",
            "timeout": timeout,
        }
        return self._call(payload, timeout)

    def submit_project(self, project_dir, entrypoint, method, path, query, body, timeout=0, request_id="", slug="", request_meta=""):
        timeout = timeout if timeout and timeout > 0 else int(os.environ.get("VLAMBDA_INVOKE_TIMEOUT", "30"))
        payload = {
            "kind": "project",
            "project_dir": project_dir,
            "entrypoint": entrypoint,
            "method": method,
            "path": path,
            "query_string": query,
            "body_b64": base64.b64encode(body or b"").decode("ascii"),
            "request_id": request_id or "",
            "slug": slug or "",
            "request_meta": request_meta or "",
            "timeout": timeout,
        }
        return self._call(payload, timeout)

    def _call(self, payload: dict, timeout: int):
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()
            try:
                self._proc.stdin.write(json.dumps(payload) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._start()
                return {"error": "fork supervisor unavailable"}
            ready, _, _ = select.select([self._proc.stdout], [], [], timeout + 4)
            if not ready:
                try:
                    self._proc.kill()
                except Exception:
                    pass
                self._proc = None
                return {"error": "invocation timeout"}
            line = self._proc.stdout.readline()
            if not line:
                self._proc = None
                return {"error": "fork supervisor exited"}
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError:
                self._proc = None
                return {"error": "invalid runner response"}
            _record_runner_logs(envelope.get("logs"))
            if envelope.get("ok"):
                return envelope.get("response")
            return {"error": envelope.get("error", "execution failed")}

    def stop(self):
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass


class ForkPool:
    def __init__(self, size: int):
        self._workers = [ForkWorker() for _ in range(max(1, size))]
        self._counter = itertools.count()
        self._lock = threading.Lock()
        self._max_workers = len(self._workers)

    def execute(self, code, method, path, query, body, timeout=0, memory_mb=0, vendor_dir="", request_id="", slug="", request_meta="", code_key=""):
        with self._lock:
            workers = list(self._workers)
        idx = next(self._counter) % len(workers)
        return workers[idx].submit(code, method, path, query, body, timeout, memory_mb, vendor_dir, request_id, slug, request_meta, code_key)

    def execute_project(self, project_dir, entrypoint, method, path, query, body, timeout=0, request_id="", slug="", request_meta=""):
        with self._lock:
            workers = list(self._workers)
        idx = next(self._counter) % len(workers)
        return workers[idx].submit_project(project_dir, entrypoint, method, path, query, body, timeout, request_id, slug, request_meta)

    def _queue_depth(self) -> int:
        return 0

    def stats(self):
        return {"workers": self._max_workers, "max_workers": self._max_workers, "queue_depth": 0, "mode": "fork"}

    def stop(self):
        for worker in self._workers:
            worker.stop()


def _landlock_read_paths() -> list[str]:
    """Paths the runner needs for Python and its own code."""
    candidates = ["/opt", "/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc"]
    candidates.extend([sys.prefix, sys.base_prefix])
    candidates.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    candidates.extend(sys.path)
    paths: list[str] = []
    for path in candidates:
        if not path or path in (".", "/") or not os.path.isdir(path):
            continue
        normalized = os.path.abspath(path)
        if normalized == "/":
            continue
        if normalized == "/data" or normalized.startswith("/data/"):
            continue
        if normalized not in paths:
            paths.append(normalized)
    broker_dir = os.environ.get("VLRUN_BROKER_DIR") or os.path.dirname(
        os.environ.get("VLAMBDA_BROKER_SOCKET") or (
            "/data/broker/vlambda.sock" if os.path.isdir("/data") else "/tmp/opencode/vlambda-broker.sock"
        )
    )
    if broker_dir and os.path.isdir(broker_dir) and os.path.abspath(broker_dir) not in paths:
        paths.append(os.path.abspath(broker_dir))
    return paths


class LandlockWorker:
    """Run one invocation in a fresh Landlock + seccomp sandbox."""

    def submit(self, code, method, path, query, body, timeout=0, memory_mb=0, vendor_dir="", request_id="", slug="", request_meta="", code_key=""):
        vendor = vendor_dir or ""
        if vendor and not os.path.isdir(vendor):
            vendor = ""
        payload = {
            "kind": "code",
            "code": code,
            "method": method,
            "path": path,
            "query_string": query,
            "body_b64": base64.b64encode(body or b"").decode("ascii"),
            "vendor_dir": vendor,
            "request_id": request_id or "",
            "slug": slug or "",
            "request_meta": request_meta or "",
            "code_key": code_key or "",
        }
        return self._run(payload, timeout, vendor_dir=vendor)

    def submit_project(self, project_dir, entrypoint, method, path, query, body, timeout=0, request_id="", slug="", request_meta=""):
        payload = {
            "kind": "project",
            "project_dir": project_dir,
            "entrypoint": entrypoint,
            "method": method,
            "path": path,
            "query_string": query,
            "body_b64": base64.b64encode(body or b"").decode("ascii"),
            "request_id": request_id or "",
            "slug": slug or "",
            "request_meta": request_meta or "",
        }
        return self._run(payload, timeout, project_dir=project_dir)

    def _run(self, payload: dict, timeout: int = 0, project_dir: str = "", vendor_dir: str = ""):
        timeout = timeout if timeout and timeout > 0 else int(os.environ.get("VLAMBDA_INVOKE_TIMEOUT", "30"))
        tmp_dir = tempfile.mkdtemp(prefix="vlambda-run-", dir="/tmp")
        try:
            read_paths = _landlock_read_paths()
            if project_dir:
                read_paths.append(project_dir)
            if vendor_dir:
                read_paths.append(vendor_dir)
            cmd = [sys.executable, "-m", "vlambda.runner", "--sandbox", "landlock"]
            for path in dict.fromkeys(read_paths):
                cmd.extend(["--allow-read", path])
            cmd.extend(["--allow-rw", tmp_dir, "--cpu", str(max(1, int(timeout)))])
            env = _runner_env()
            env["HOME"] = tmp_dir
            env["TMPDIR"] = tmp_dir
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=tmp_dir,
                env=env,
            )
            try:
                out, err = proc.communicate(json.dumps(payload) + "\n", timeout=timeout + 2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                return {"error": "invocation timeout"}
        except Exception as e:
            print(f"landlock setup failed: {e}", file=sys.stderr, flush=True)
            return {"error": "sandbox failed"}
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        lines = [line for line in out.splitlines() if line.strip()]
        if not lines:
            if err.strip():
                print(f"landlock error: {err.strip()[-2000:]}", file=sys.stderr, flush=True)
            return {"error": "sandbox exited"}
        try:
            envelope = json.loads(lines[-1])
        except json.JSONDecodeError:
            return {"error": "invalid runner response"}
        _record_runner_logs(envelope.get("logs"))
        if envelope.get("ok"):
            return envelope.get("response")
        return {"error": envelope.get("error", "execution failed")}


class LandlockPool:
    def __init__(self, size: int):
        self._sem = threading.BoundedSemaphore(max(1, size))
        self._max_workers = max(1, size)

    def execute(self, code, method, path, query, body, timeout=0, memory_mb=0, vendor_dir="", request_id="", slug="", request_meta="", code_key=""):
        with self._sem:
            return LandlockWorker().submit(code, method, path, query, body, timeout, memory_mb, vendor_dir, request_id, slug, request_meta, code_key)

    def execute_project(self, project_dir, entrypoint, method, path, query, body, timeout=0, request_id="", slug="", request_meta=""):
        with self._sem:
            return LandlockWorker().submit_project(project_dir, entrypoint, method, path, query, body, timeout, request_id, slug, request_meta)

    def _queue_depth(self) -> int:
        return 0

    def stats(self):
        return {"workers": self._max_workers, "max_workers": self._max_workers, "queue_depth": 0, "mode": "landlock"}

    def stop(self):
        pass


def _nsjail_payload(payload: dict) -> tuple[dict, str]:
    """Map the broker socket into the nsjail mount namespace."""
    broker_dir = ""
    try:
        meta = json.loads(payload.get("request_meta", "{}") or "{}")
        if isinstance(meta, dict):
            broker_socket = str(meta.get("broker_socket", ""))
            if broker_socket:
                broker_dir = os.path.dirname(broker_socket)
                meta["broker_socket"] = "/broker/" + os.path.basename(broker_socket)
                payload = dict(payload)
                payload["request_meta"] = json.dumps(meta)
    except Exception:
        pass
    return payload, broker_dir


class NsjailWorker:
    """Run one invocation in a fresh nsjail sandbox."""
    def submit(self, code, method, path, query, body, timeout=0, memory_mb=0, vendor_dir="", request_id="", slug="", request_meta="", code_key=""):
        vendor = vendor_dir or ""
        if vendor and not os.path.isdir(vendor):
            vendor = ""
        payload = {
            "kind": "code",
            "code": code,
            "method": method,
            "path": path,
            "query_string": query,
            "body_b64": base64.b64encode(body or b"").decode("ascii"),
            "vendor_dir": "/vendor" if vendor else "",
            "request_id": request_id or "",
            "slug": slug or "",
            "request_meta": request_meta or "",
            "code_key": code_key or "",
        }
        payload, broker_dir = _nsjail_payload(payload)
        return self._run(payload, timeout, vendor_dir=vendor, broker_dir=broker_dir)

    def submit_project(self, project_dir, entrypoint, method, path, query, body, timeout=0, request_id="", slug="", request_meta=""):
        payload = {
            "kind": "project",
            "project_dir": "/work",
            "entrypoint": entrypoint,
            "method": method,
            "path": path,
            "query_string": query,
            "body_b64": base64.b64encode(body or b"").decode("ascii"),
            "request_id": request_id or "",
            "slug": slug or "",
            "request_meta": request_meta or "",
        }
        payload, broker_dir = _nsjail_payload(payload)
        return self._run(payload, timeout, project_dir=project_dir, broker_dir=broker_dir)

    def _run(self, payload: dict, timeout: int = 0, project_dir: str = "", vendor_dir: str = "", broker_dir: str = ""):
        nsjail = shutil.which("nsjail") or "/usr/local/bin/nsjail"
        if not os.path.exists(nsjail):
            return {"error": "sandbox unavailable"}
        timeout = timeout if timeout and timeout > 0 else int(os.environ.get("VLAMBDA_INVOKE_TIMEOUT", "30"))
        config_path = ""
        try:
            fd, config_path = tempfile.mkstemp(prefix="vlambda-", suffix=".cfg", dir="/tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(_nsjail_config(project_dir, vendor_dir, broker_dir))
            proc = subprocess.Popen(
                [nsjail, "--config", config_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=_runner_env(),
            )
            try:
                out, err = proc.communicate(json.dumps(payload) + "\n", timeout=timeout + 2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                return {"error": "invocation timeout"}
        except Exception as e:
            print(f"nsjail setup failed: {e}", file=sys.stderr, flush=True)
            return {"error": "sandbox failed"}
        finally:
            if config_path:
                try:
                    os.unlink(config_path)
                except OSError:
                    pass
        lines = [line for line in out.splitlines() if line.strip()]
        if not lines:
            if err.strip():
                print(f"nsjail error: {err.strip()[-2000:]}", file=sys.stderr, flush=True)
            return {"error": "sandbox exited"}
        try:
            envelope = json.loads(lines[-1])
        except json.JSONDecodeError:
            return {"error": "invalid runner response"}
        _record_runner_logs(envelope.get("logs"))
        if envelope.get("ok"):
            return envelope.get("response")
        return {"error": envelope.get("error", "execution failed")}


class NsjailPool:
    def __init__(self, size: int):
        self._sem = threading.BoundedSemaphore(max(1, size))
        self._max_workers = max(1, size)

    def execute(self, code, method, path, query, body, timeout=0, memory_mb=0, vendor_dir="", request_id="", slug="", request_meta="", code_key=""):
        with self._sem:
            return NsjailWorker().submit(code, method, path, query, body, timeout, memory_mb, vendor_dir, request_id, slug, request_meta, code_key)

    def execute_project(self, project_dir, entrypoint, method, path, query, body, timeout=0, request_id="", slug="", request_meta=""):
        with self._sem:
            return NsjailWorker().submit_project(project_dir, entrypoint, method, path, query, body, timeout, request_id, slug, request_meta)

    def _queue_depth(self) -> int:
        return 0

    def stats(self):
        return {"workers": self._max_workers, "max_workers": self._max_workers, "queue_depth": 0, "mode": "nsjail"}

    def stop(self):
        pass


_sandbox_mode = os.environ.get("VLAMBDA_SANDBOX", "").lower()
if _sandbox_mode in ("fork", "forklandlock"):
    Pool = ForkPool
elif _sandbox_mode == "landlock":
    Pool = LandlockPool
elif _sandbox_mode == "nsjail" or os.environ.get("VLAMBDA_NSJAIL", "0") == "1":
    Pool = NsjailPool
elif _sandbox_mode == "process" or os.environ.get("VLAMBDA_ISOLATE", "0") == "1":
    Pool = ProcessPool
elif sys.version_info >= (3, 15):
    # CPython 3.15 can crash when handlers run on server threads; isolate them.
    Pool = ProcessPool
else:
    Pool = InProcessPool
