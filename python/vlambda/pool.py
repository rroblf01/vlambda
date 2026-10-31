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
from typing import Any

from vlambda.sandbox import execute_lambda, make_sandbox
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
            code, method, path, query, body, result_queue = task
            try:
                req_obj = Request(
                    method=method, path=path, query_string=query,
                    headers={}, body=body,
                )
                compiled, ns_template, handler = get_compiled(code)
                if handler is None:
                    result = {"error": "no handler function defined"}
                else:
                    ns = dict(ns_template)
                    ns["request"] = req_obj
                    result = handler(req_obj)
            except Exception as e:
                result = {"error": f"{type(e).__name__}: {e}"}
            result_queue.put(result)

    # Single-flight: coalesce identical in-flight requests
    _inflight: dict = {}
    _inflight_lock = threading.Lock()

    _SINGLE_FLIGHT = __import__("os").environ.get("VLAMBDA_SINGLE_FLIGHT", "0") == "1"

    def submit(self, code, method, path, query, body, timeout=0, memory_mb=0) -> Any:
        if memory_mb and memory_mb > 0:
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
            if usage > memory_mb * 1024 * 1024:
                return {"error": "memory quota exceeded"}
        if not Worker._SINGLE_FLIGHT:
            result_queue = queue.Queue(1)
            try:
                self._queue.put((code, method, path, query, body, result_queue), timeout=5)
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
        try:
            self._queue.put((code, method, path, query, body, result_queue), timeout=5)
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

    def stop(self):
        self._queue.put(None)


class InProcessPool:
    def __init__(self, size: int):
        self._max_workers = max(size, int(os.environ.get("VLAMBDA_MAX_WORKERS", "64")))
        self._workers = [Worker(i) for i in range(size)]
        self._lock = threading.Lock()
        self._counter = itertools.count()

    def execute(self, code, method, path, query, body, timeout=0, memory_mb=0) -> Any:
        # Auto-scale: if queue is deep, add a worker up to the configured cap.
        with self._lock:
            if len(self._workers) < self._max_workers and self._queue_depth() > len(self._workers) * 2:
                self._workers.append(Worker(len(self._workers)))
            workers = list(self._workers)
        # Atomic round-robin selection without holding the lock during submit.
        idx = next(self._counter) % len(workers)
        return workers[idx].submit(code, method, path, query, body, timeout, memory_mb)

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
    """Return the command used to start an isolated runner process."""
    python = sys.executable
    if os.environ.get("VLAMBDA_NSJAIL", "0") != "1":
        return [python, "-m", "vlambda.runner"]
    nsjail = shutil.which("nsjail")
    if not nsjail:
        return [python, "-m", "vlambda.runner"]
    extra = os.environ.get("VLAMBDA_NSJAIL_ARGS", "").split()
    return [
        nsjail,
        "--mode", "o",
        "--chroot", "/",
        "--disable_clone_newnet",
        "--disable_proc",
        "--rlimit_as", os.environ.get("VLAMBDA_NSJAIL_AS_MB", "80"),
        "--rlimit_cpu", os.environ.get("VLAMBDA_NSJAIL_CPU", "5"),
        "--rlimit_nofile", "64",
        "--rlimit_nproc", "16",
        "--time_limit", os.environ.get("VLAMBDA_NSJAIL_TIME", "5"),
        "--tmpfs", "/tmp",
        *extra,
        "--",
        python, "-m", "vlambda.runner",
    ]


class ProcessWorker:
    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._start()

    def _start(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
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

    def submit(self, code, method, path, query, body, timeout=0, memory_mb=0):
        timeout = timeout if timeout and timeout > 0 else int(os.environ.get("VLAMBDA_INVOKE_TIMEOUT", "30"))
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()
            payload = {
                "code": code,
                "method": method,
                "path": path,
                "query_string": query,
                "body_b64": base64.b64encode(body or b"").decode("ascii"),
            }
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
            if envelope.get("ok"):
                return envelope.get("payload")
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


class ProcessPool:
    def __init__(self, size: int):
        self._workers = [ProcessWorker() for _ in range(max(1, size))]
        self._counter = itertools.count()
        self._lock = threading.Lock()
        self._max_workers = len(self._workers)

    def execute(self, code, method, path, query, body, timeout=0, memory_mb=0):
        with self._lock:
            workers = list(self._workers)
        idx = next(self._counter) % len(workers)
        return workers[idx].submit(code, method, path, query, body, timeout, memory_mb)

    def _queue_depth(self) -> int:
        return 0

    def stats(self):
        return {"workers": len(self._workers), "max_workers": self._max_workers, "queue_depth": 0, "mode": "process"}

    def stop(self):
        for worker in self._workers:
            worker.stop()


if os.environ.get("VLAMBDA_ISOLATE", "0") == "1":
    Pool = ProcessPool
else:
    Pool = InProcessPool
