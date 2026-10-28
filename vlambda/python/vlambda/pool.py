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
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            task = self._queue.get()
            if task is None:
                break
            code, method, path, query, body, result_queue = task
            try:
                compiled, ns_template = get_compiled(code)
                # Create a fresh namespace from template
                ns = dict(ns_template)
                ns["request"] = Request(
                    method=method, path=path, query_string=query,
                    headers={}, body=body,
                )
                exec(compiled, ns)
                handler = ns.get("handler")
                if handler is None:
                    result = {"error": "no handler function defined"}
                else:
                    result = handler(ns["request"])
            except Exception as e:
                result = {"error": f"{type(e).__name__}: {e}"}
            result_queue.put(result)

    def submit(self, code, method, path, query, body) -> Any:
        result_queue = queue.Queue(1)
        self._queue.put((code, method, path, query, body, result_queue))
        return result_queue.get(timeout=30)

    def stop(self):
        self._queue.put(None)


class Pool:
    def __init__(self, size: int):
        self._workers = [Worker(i) for i in range(size)]
        self._index = 0
        self._lock = threading.Lock()

    def execute(self, code, method, path, query, body) -> Any:
        with self._lock:
            worker = self._workers[self._index % len(self._workers)]
            self._index += 1
        return worker.submit(code, method, path, query, body)

    def stop(self):
        for w in self._workers:
            w.stop()
