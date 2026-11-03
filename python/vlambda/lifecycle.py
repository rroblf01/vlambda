"""Graceful shutdown and in-flight request accounting."""

from __future__ import annotations

import os
import threading
import time

_lock = threading.Lock()
_condition = threading.Condition(_lock)
_inflight = 0
_accepting = True


def begin() -> bool:
    global _inflight
    with _condition:
        if not _accepting:
            return False
        _inflight += 1
        return True


def end() -> None:
    global _inflight
    with _condition:
        if _inflight > 0:
            _inflight -= 1
        _condition.notify_all()


def stop_accepting() -> None:
    global _accepting
    with _condition:
        _accepting = False


def accepting() -> bool:
    with _condition:
        return _accepting


def inflight() -> int:
    with _condition:
        return _inflight


def reset() -> None:
    """Test helper: restore the accepting state."""
    global _inflight, _accepting
    with _condition:
        _inflight = 0
        _accepting = True
        _condition.notify_all()


def drain(timeout: float | None = None) -> bool:
    if timeout is None:
        try:
            timeout = float(os.environ.get("VLAMBDA_DRAIN_TIMEOUT", "20"))
        except ValueError:
            timeout = 20.0
    deadline = time.monotonic() + max(0.0, timeout)
    with _condition:
        while _inflight > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _condition.wait(remaining)
        return True
