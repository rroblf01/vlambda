def handler(request):
    """Small JSON API. Enable CORS with VLAMBDA_CORS to add headers to /f/<slug>."""
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
