"""Python worker pool for executing lambdas.

Each worker runs in its own thread with its own sandbox namespace.
Code is compiled once and cached. V dispatches requests to available workers.
"""

from __future__ import annotations

import threading
import time
import queue
from typing import Any

from vlambda.sandbox import execute_lambda, make_sandbox
from vlambda.request import Request
from vlambda.cache import get as get_compiled


class Worker:
    def __init__(self, worker_id: int):
        self.id = worker_id
        self._queue: queue.Queue = queue.Queue(maxsize=1000)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            task = self._queue.get()
            if task is None:
                break
            code, method, path, query, body, result_queue = task
            try:
                # Response cache for GET requests
                from vlambda.cache import get_response, set_response
                cache_key = None
                if method == "GET":
                    cache_key = f"{code}:{path}:{query}"
                    cached = get_response(cache_key)
                    if cached is not None:
                        result = cached
                        result_queue.put(result)
                        continue

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

                if cache_key is not None:
                    set_response(cache_key, result)
            except Exception as e:
                result = {"error": f"{type(e).__name__}: {e}"}
            result_queue.put(result)

    # Single-flight: coalesce identical in-flight requests
    _inflight: dict = {}
    _inflight_lock = threading.Lock()

    _SINGLE_FLIGHT = __import__("os").environ.get("VLAMBDA_SINGLE_FLIGHT", "0") == "1"

    def submit(self, code, method, path, query, body) -> Any:
        if not Worker._SINGLE_FLIGHT:
            result_queue = queue.Queue(1)
            try:
                self._queue.put((code, method, path, query, body, result_queue), timeout=5)
            except queue.Full:
                return {"error": "server busy, try again later"}
            try:
                return result_queue.get(timeout=30)
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
            event.wait(timeout=30)
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
            result = result_queue.get(timeout=30)
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
        self._workers = [Worker(i) for i in range(size)]
        self._index = 0
        self._lock = threading.Lock()

    def execute(self, code, method, path, query, body) -> Any:
        # Auto-scale: if queue is deep, add a worker
        with self._lock:
            if len(self._workers) < 64 and self._queue_depth() > len(self._workers) * 2:
                self._workers.append(Worker(len(self._workers)))
            worker = self._workers[self._index % len(self._workers)]
            self._index += 1
        return worker.submit(code, method, path, query, body)

    def _queue_depth(self) -> int:
        return sum(w._queue.qsize() for w in self._workers)

    def stop(self):
        for w in self._workers:
            w.stop()
