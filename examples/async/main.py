"""Async handler example.

vlambda awaits the coroutine returned by an async handler, so helpers written
with `async def` can be reused directly.
"""


async def lookup(item_id):
    # Stand-in for an async database or HTTP client call.
    return {"id": item_id, "name": f"item-{item_id}"}


async def handler(request):
    items = [await lookup(i) for i in range(1, 4)]
    return {"async": True, "items": items, "path": request.path}
