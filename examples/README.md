# vlambda examples

Small proofs of concept that run on vlambda 0.4.0 without extra state,
databases or external services.

| Example | What it shows | How to deploy |
|---|---|---|
| [`hello`](hello/) | Minimal JSON lambda | Create a lambda and paste `main.py` |
| [`json-api`](json-api/) | JSON API with automatic CORS | Create a `GET` lambda |
| [`webhook`](webhook/) | Receive and log a webhook | Create a `POST` lambda |
| [`html-fetch`](html-fetch/) | HTML page plus `fetch()` to its own API | Create a `GET` lambda |
| [`static-site`](static-site/) | Static files served by a project | Upload the folder as a ZIP |
| [`async`](async/) | Async handler with `await` | Create a `GET` lambda |

## Deploy from the UI

1. Open vlambda and log in.
2. **New lambda** for the single-file examples, or **Upload project** for
   `static-site`.
3. Paste the code (or ZIP the folder) and create it.
4. Open the invoke URL. For `static-site`, use `/f/<slug>/`.

## Deploy from the API

```bash
# Single-file lambda
curl -X POST "$VLAMBDA_URL/vlambda?name=hello&method=GET" \
  -H "Authorization: Bearer $VLAMBDA_TOKEN" \
  --data-binary @examples/hello/main.py

# Project (ZIP the contents, not the folder)
cd examples/static-site && zip -r /tmp/static-site.zip .
base64 -w0 /tmp/static-site.zip > /tmp/static-site.b64
curl -X POST "$VLAMBDA_URL/vlambda?project=1&entry=main.py&name=static-site" \
  -H "Authorization: Bearer $VLAMBDA_TOKEN" \
  --data-binary @/tmp/static-site.b64
```

`VLAMBDA_TOKEN` can be a JWT from `/login` or a long-lived API token created
in **System → API tokens**.

## Files and static serving

Projects are served at `/f/<slug>/<path>`:

- `/f/<slug>/` serves `index.html` when it exists.
- Any existing project file (`style.css`, `app.js`, images…) is returned with
  its content type.
- Any other path falls through to `handler(request)`, so a project can mix a
  static frontend with JSON endpoints.
- `vendor/`, `__pycache__/`, `.git/` and path traversal (`..`) are blocked.
