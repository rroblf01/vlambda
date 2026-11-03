import json


def handler(request):
    """Single lambda that serves an HTML page and a JSON endpoint.

    GET /f/<slug>       -> HTML page
    GET /f/<slug>/api   -> JSON consumed by the page with fetch()
    """
    if request.path.endswith("/api"):
        return {"items": ["alpha", "beta", "gamma"], "path": request.path}

    items = json.dumps(["alpha", "beta", "gamma"])
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>vlambda fetch demo</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 640px; margin: 40px auto; padding: 0 16px; }
    li { padding: 4px 0; }
    button { padding: 8px 14px; cursor: pointer; }
  </style>
</head>
<body>
  <h1>vlambda fetch demo</h1>
  <p>This page calls its own lambda with <code>fetch()</code>.</p>
  <button id="load">Load items</button>
  <ul id="list"></ul>
  <script>
    document.getElementById('load').addEventListener('click', async () => {
      const res = await fetch('api');
      const data = await res.json();
      document.getElementById('list').innerHTML =
        data.items.map(item => '<li>' + item + '</li>').join('');
    });
  </script>
  <script>window.__seed = %s;</script>
</body>
</html>""" % items
