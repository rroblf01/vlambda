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


class Pool:
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
