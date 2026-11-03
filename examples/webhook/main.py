import json


def handler(request):
    """Receive a webhook and log a compact copy of the payload."""
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        payload = {"raw": request.body.decode("utf-8", "replace")}
    request.log("webhook received: " + json.dumps(payload)[:200])
    return {"received": True, "payload": payload}
