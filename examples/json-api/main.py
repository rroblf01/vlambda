def handler(request):
    """Small JSON API. vlambda adds CORS headers to /f/<slug> responses."""
    return {
        "ok": True,
        "method": request.method,
        "path": request.path,
        "query": request.query_string,
        "items": [
            {"id": 1, "name": "first"},
            {"id": 2, "name": "second"},
        ],
    }
