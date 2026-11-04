"""Run handlers that are declared with ``async def``.

Handlers may be plain functions or coroutine functions.  The invocation
threads in vlambda are not asyncio-based, so an awaitable result is driven to
completion on a private event loop before it reaches the response serializer.
"""

from __future__ import annotations

import asyncio
import inspect
import threading


async def _await_result(result):
    return await result


def resolve(result):
    """Return the plain value of a handler result, awaiting it if needed."""
    if not inspect.isawaitable(result):
        return result

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_await_result(result))

    # A loop is already running on this thread (an embedder may call the
    # runner from asyncio).  Never nest loops: drive the coroutine on a
    # private loop in a short-lived thread instead.
    values: list[object] = []
    errors: list[BaseException] = []

    def _run():
        try:
            values.append(asyncio.run(_await_result(result)))
        except BaseException as exc:  # re-raised on the caller's thread
            errors.append(exc)

    thread = threading.Thread(target=_run, name="vlambda-async-handler", daemon=True)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return values[0] if values else None
