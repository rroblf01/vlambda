def handler(request):
    """Fallback handler for API calls that are not static files."""
    return {"ok": True, "path": request.path}
