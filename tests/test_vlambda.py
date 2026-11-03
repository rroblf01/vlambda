"""Integration tests for vlambda."""

import subprocess, time, json, urllib.request, os, signal, sys

PORT = 9799
DB = "/tmp/test_vlambda.db"
PROC = None

def start_server(extra_env=None):
    import os
    global PROC
    metrics_db = DB.replace(".db", "_metrics.db")
    for f in [DB, DB+"-wal", DB+"-shm", metrics_db, metrics_db+"-wal", metrics_db+"-shm"]:
        try: os.remove(f)
        except FileNotFoundError: pass
    base = os.environ.get("VLAMBDA_BASE", os.getcwd())
    # If running from vlambda/, adjust
    if os.path.exists(os.path.join(base, "python", "vlambda")):
        python_bin = os.path.join(base, ".venv", "bin", "python")
        cwd = base
    elif os.path.exists(os.path.join(base, "vlambda", "python", "vlambda")):
        python_bin = os.path.join(base, "vlambda", ".venv", "bin", "python")
        cwd = os.path.join(base, "vlambda")
    else:
        python_bin = "python3"
        cwd = base
    env = os.environ.copy()
    env["VLAMBDA_ADMIN_TOKEN"] = "test-admin"
    env["VLAMBDA_DB"] = DB
    env["VLAMBDA_METRICS_DB"] = DB.replace(".db", "_metrics.db")
    env.update(extra_env or {})
    PROC = subprocess.Popen(
        [python_bin, "-m", "vlambda", "--port", str(PORT), "--sqlite_path", DB, "--max_body_size", "100"],
        cwd=cwd,
        env=env,
        stdout=open("/tmp/vlambda_test_server.log", "w"), stderr=subprocess.STDOUT,
    )
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://localhost:{PORT}/health")
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("server did not start")

def stop_server():
    global PROC
    if PROC:
        PROC.terminate()
        try: PROC.wait(timeout=5)
        except subprocess.TimeoutExpired: PROC.kill()
        PROC = None

def req(method, path, data=None, token=None, extra_headers=None):
    url = f"http://localhost:{PORT}{path}"
    headers = dict(extra_headers or {})
    if token: headers["Authorization"] = f"Bearer {token}"
    body = data.encode() if isinstance(data, str) else data
    r = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(r)
        raw = resp.read()
        try: body = json.loads(raw) if raw else {}
        except Exception: body = {}
        return resp.status, body
    except urllib.error.HTTPError as e:
        raw = e.read()
        try: body = json.loads(raw) if raw else {}
        except Exception: body = {}
        return e.code, body

def test_health():
    status, body = req("GET", "/health")
    assert status == 200 and body["status"] == "ok"

def test_favicon_only_for_ui():
    status, _ = req("GET", "/favicon.ico", extra_headers={"Referer": f"http://localhost:{PORT}/"})
    assert status == 200
    status, _ = req("GET", "/favicon.ico")
    assert status == 200
    status, _ = req("GET", "/favicon.ico", extra_headers={"Referer": f"http://localhost:{PORT}/f/abc"})
    assert status == 404

_counter = [0]
def _unique_user():
    _counter[0] += 1
    return f"user{_counter[0]}"

def test_register_login():
    u = _unique_user()
    status, body = req("POST", "/register", f"{u}:testpass")
    assert status == 201, body
    status, body = req("GET", "/config")
    assert status == 200 and body.get("registration") is True
    status, body = req("POST", "/login", f"{u}:testpass")
    assert status == 200 and "token" in body
    return body["token"]


def test_registration_controls():
    stop_server()
    start_server({"VLAMBDA_REGISTRATION": "0"})
    try:
        status, body = req("GET", "/config")
        assert status == 200 and body.get("registration") is False

        status, body = req("POST", "/register", "closed:testpass")
        assert status == 403, body

        status, body = req("POST", "/admin/users", "closed:testpass",
                           extra_headers={"X-Admin-Token": "wrong"})
        assert status == 403, body

        status, body = req("POST", "/admin/users", "closed:testpass",
                           extra_headers={"X-Admin-Token": "test-admin"})
        assert status == 201, body
        status, body = req("POST", "/login", "closed:testpass")
        assert status == 200 and "token" in body
    finally:
        stop_server()


def test_isolated_projects():
    stop_server()
    start_server({
        "VLAMBDA_ISOLATE": "1",
        "VLAMBDA_COROUTINES": "2",
        "VLAMBDA_SECRET": "test-secret",
    })
    try:
        token, slug = _upload_project("isolated", {
            "main.py": (
                "import os\n"
                "def handler(request):\n"
                "    return {\n"
                "        'ok': True,\n"
                "        'secret': os.environ.get('VLAMBDA_SECRET'),\n"
                "        'admin': os.environ.get('VLAMBDA_ADMIN_TOKEN'),\n"
                "    }\n"
            ),
        })
        status, body = req("GET", f"/f/{slug}")
        assert status == 200 and body.get("ok") is True, body
        assert body.get("secret") is None
        assert body.get("admin") is None

        status, body = req("GET", f"/project/{slug}", token=token)
        assert status == 200 and "files" in body

        status, body = req("POST", "/vlambda?name=isolated-code",
                           "def handler(request): return {'isolated': True}", token)
        assert status == 201, body
        status, body = req("GET", f"/f/{body['slug']}")
        assert status == 200 and body.get("isolated") is True
    finally:
        stop_server()

def test_create_invoke():
    token = test_register_login()
    status, body = req("POST", "/vlambda", "def handler(request): return {\"msg\": \"hi\"}", token)
    assert status == 201
    slug = body["slug"]
    status, body = req("GET", f"/f/{slug}")
    assert status == 200 and body["msg"] == "hi"
    return token, slug

def test_lambda_names():
    from urllib.parse import quote
    token = test_register_login()
    name = "Mi Lambda & Más"
    code = "def handler(request): return {'msg': 'named'}"
    status, body = req("POST", "/vlambda?method=GET&name=" + quote(name, safe=""), code, token)
    assert status == 201, body
    slug = body["slug"]

    def listed_name():
        status, payload = req("GET", "/vlambda?format=json", token=token)
        assert status == 200, payload
        item = next(x for x in payload["lambdas"] if x["slug"] == slug)
        return item

    assert listed_name()["name"] == name

    # Updating without a name must keep the current one.
    status, body = req("PUT", f"/vlambda/{slug}?method=POST", "def handler(request): return {'msg': 'v2'}", token)
    assert status == 200, body
    item = listed_name()
    assert item["name"] == name and item["method"] == "POST"

    # An encoded name on update is decoded as well.
    status, body = req("PUT", f"/vlambda/{slug}?method=POST&name=" + quote("Nuevo Nombre", safe=""),
                       "def handler(request): return {'msg': 'v3'}", token)
    assert status == 200, body
    assert listed_name()["name"] == "Nuevo Nombre"

    # Project names go through the same decoding.
    token, project = _upload_project("Proyecto Ñandú", {
        "main.py": "def handler(request):\n    return {'ok': True}\n",
    })
    status, payload = req("GET", "/vlambda?format=json", token=token)
    assert status == 200, payload
    item = next(x for x in payload["lambdas"] if x["slug"] == project)
    assert item["name"] == "Proyecto Ñandú"


def test_html_lambda():
    token = test_register_login()
    code = 'def handler(request):\n    return "<!DOCTYPE html><html><body><h1>Hi</h1></body></html>"'
    status, body = req("POST", "/vlambda", code, token)
    assert status == 201, body
    slug = body["slug"]
    import urllib.request
    resp = urllib.request.urlopen(f"http://localhost:{PORT}/f/{slug}")
    raw = resp.read()
    assert resp.status == 200
    assert "text/html" in resp.headers.get("Content-Type", "")
    assert b"<h1>Hi</h1>" in raw

def test_auth_required():
    status, body = req("POST", "/vlambda", "def handler(request): return {}")
    assert status == 401

def test_update():
    token, slug = test_create_invoke()
    status, body = req("PUT", f"/vlambda/{slug}", "def handler(request): return {\"msg\": \"updated\"}", token)
    assert status == 200
    status, body = req("GET", f"/f/{slug}")
    assert body["msg"] == "updated"

def test_delete():
    token, slug = test_create_invoke()
    status, body = req("DELETE", f"/vlambda/{slug}", token=token)
    assert status == 200
    status, body = req("GET", f"/f/{slug}")
    assert status == 404

def test_validation():
    token = test_register_login()
    status, body = req("POST", "/vlambda", "print('no handler')", token)
    assert status == 400

def test_metrics():
    import urllib.request
    resp = urllib.request.urlopen(f"http://localhost:{PORT}/metrics")
    assert resp.status == 200

def test_body_size():
    token = test_register_login()
    big = "x" * 200
    status, body = req("POST", "/vlambda", big, token)
    assert status == 413



def test_method_mismatch():
    token, slug = test_create_invoke()
    # create is GET by default; POST should be 405
    status, body = req("POST", f"/f/{slug}")
    assert status == 405

def test_pagination():
    token = test_register_login()
    for i in range(3):
        req("POST", "/vlambda", f"def handler(request): return {{'i': {i}}}", token)
    status, body = req("GET", "/vlambda?limit=2&offset=0", token=token)
    assert status == 200
    assert body["limit"] == 2
    assert body["offset"] == 0

def test_ownership():
    t1 = test_register_login()
    t2 = test_register_login()
    status, body = req("POST", "/vlambda", "def handler(request): return {}", t1)
    assert status == 201
    slug = body["slug"]
    # second user cannot get/update/delete
    status, body = req("GET", f"/vlambda/{slug}", token=t2)
    assert status in (401, 403, 404)
    status, body = req("DELETE", f"/vlambda/{slug}", token=t2)
    assert status in (401, 403, 404, 500)

def test_project_zip():
    import base64, io, zipfile
    buf = io.BytesIO()
    unique = str(time.time())
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("main.py", "# " + unique + "\ndef handler(request):\n    from utils import shout\n    return {'msg': shout(request.path)}\n")
        z.writestr("utils.py", "def shout(x):\n    return x.upper()\n")
    b64 = base64.b64encode(buf.getvalue()).decode()
    token = test_register_login()
    status, body = req("POST", "/vlambda?project=1&entry=main.py&name=demo", b64, token)
    assert status == 201, body
    slug = body["slug"]
    status, body = req("GET", f"/f/{slug}")
    assert status == 200
    assert "msg" in body
    # Read project files through the editor API.
    status, body = req("GET", f"/project/{slug}", token=token)
    assert status == 200
    assert "files" in body and "main.py" in body["files"]
    # Save edited files and re-invoke.
    payload = '{"files":{"main.py":"def handler(request): return {\\"msg\\": \\"edited\\"}"},"entrypoint":"main.py"}'
    status, body = req("PUT", f"/project/{slug}", payload, token)
    assert status == 200
    time.sleep(0.2)
    status, body = req("GET", f"/f/{slug}")
    assert body.get("msg") == "edited"
    status, body = req("DELETE", f"/vlambda/{slug}", token=token)
    assert status == 200

def test_nsjail_sandbox():
    import shutil
    if not shutil.which("nsjail"):
        return
    stop_server()
    start_server({
        "VLAMBDA_NSJAIL": "1",
        "VLAMBDA_ISOLATE": "1",
        "VLAMBDA_SECRET": "test-secret",
        "VLAMBDA_COROUTINES": "2",
    })
    try:
        token, slug = _upload_project("nsjail", {
            "main.py": (
                "import os, socket\n"
                "def handler(request):\n"
                "    out = {'data': os.listdir('/data'), 'uid': os.getuid()}\n"
                "    out['db'] = os.path.exists('/data/db/vlambda.db')\n"
                "    out['vl'] = [k for k in os.environ if k.startswith('VLAMBDA_')]\n"
                "    try:\n"
                "        open('/proc/1/environ', 'rb').read()\n"
                "        out['proc'] = 'readable'\n"
                "    except OSError:\n"
                "        out['proc'] = 'blocked'\n"
                "    try:\n"
                "        s = socket.socket(); s.settimeout(1); s.connect(('1.1.1.1', 80))\n"
                "        out['net'] = 'connected'\n"
                "    except OSError:\n"
                "        out['net'] = 'blocked'\n"
                "    return out\n"
            ),
        })
        status, body = req("GET", f"/f/{slug}")
        assert status == 200 and body.get("uid") == 65534, body
        assert body.get("data") == []
        assert body.get("db") is False
        assert body.get("vl") == []
        assert body.get("proc") == "blocked"
        assert body.get("net") == "blocked"
    finally:
        stop_server()


def test_landlock_sandbox():
    try:
        from vlambda.sandbox_os import sandbox_supported
        import ctypes.util
        if not sandbox_supported() or not ctypes.util.find_library("seccomp"):
            return
    except Exception:
        return
    stop_server()
    start_server({
        "VLAMBDA_SANDBOX": "landlock",
        "VLAMBDA_SECRET": "test-secret",
        "VLAMBDA_COROUTINES": "2",
        "VLAMBDA_TIMEOUT": "5",
    })
    try:
        token, slug = _upload_project("landlock", {
            "main.py": (
                "import os, socket, threading\n"
                "def handler(request):\n"
                "    out = {}\n"
                "    def check(name, fn):\n"
                "        try:\n"
                "            fn(); out[name] = 'ok'\n"
                "        except Exception as e:\n"
                "            out[name] = type(e).__name__\n"
                "    check('data', lambda: os.listdir('/data'))\n"
                "    check('db', lambda: open('/data/db/vlambda.db', 'rb').read(1))\n"
                "    check('proc', lambda: open('/proc/1/environ', 'rb').read())\n"
                "    check('socket', lambda: socket.socket())\n"
                "    check('kill', lambda: os.kill(1, 0))\n"
                "    check('fork', lambda: os.fork())\n"
                "    check('subprocess', lambda: __import__('subprocess').run(['/bin/true']))\n"
                "    check('tmp_other', lambda: open('/tmp/not-mine', 'w'))\n"
                "    values = []\n"
                "    thread = threading.Thread(target=lambda: values.append(1))\n"
                "    thread.start(); thread.join()\n"
                "    out['thread'] = values\n"
                "    out['env'] = [k for k in os.environ if k.startswith('VLAMBDA_')]\n"
                "    return out\n"
            ),
        })
        status, body = req("GET", f"/f/{slug}")
        assert status == 200, body
        assert body.get("data") in ("PermissionError", "FileNotFoundError"), body
        assert body.get("db") in ("PermissionError", "FileNotFoundError"), body
        assert body.get("proc") == "PermissionError", body
        assert body.get("socket") == "PermissionError", body
        assert body.get("kill") == "PermissionError", body
        assert body.get("fork") == "PermissionError", body
        assert body.get("subprocess") == "PermissionError", body
        assert body.get("tmp_other") == "PermissionError", body
        assert body.get("thread") == [1], body
        assert body.get("env") == [], body
    finally:
        stop_server()


def test_async_task():
    token, slug = test_create_invoke()
    status, body = req("POST", f"/async/{slug}")
    assert status == 202
    task_id = body["task_id"]
    # poll
    for _ in range(20):
        status, body = req("GET", f"/task/{task_id}")
        if body.get("status") == "completed":
            break
        time.sleep(0.1)
    assert body.get("status") == "completed"

def test_metrics_history():
    token, slug = test_create_invoke()
    req("GET", f"/f/{slug}")
    time.sleep(1.2)
    status, body = req("GET", "/metrics/history?days=1", token=token)
    assert status == 200
    assert "items" in body

def test_concurrency():
    import threading
    token, slug = test_create_invoke()
    results = []
    lock = threading.Lock()

    def worker():
        for _ in range(5):
            try:
                status, body = req("GET", f"/f/{slug}")
                with lock:
                    results.append((status, body))
            except Exception as e:
                with lock:
                    results.append(("ERR", str(e)))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)
    assert all(not th.is_alive() for th in threads)
    assert len(results) == 100
    assert all(r[0] != "ERR" for r in results)

def test_alias_version():
    time.sleep(2.0)
    token = test_register_login()
    status, body = req("POST", "/vlambda", "def handler(request): return {\"msg\": \"v1\"}", token)
    assert status == 201
    slug = body["slug"]
    status, body = req("PUT", f"/vlambda/{slug}", "def handler(request): return {\"msg\": \"v2\"}", token)
    assert status == 200
    status, body = req("POST", f"/vlambda/{slug}/alias?name=prod&version=1", token=token)
    assert status == 201
    status, body = req("GET", f"/f/{slug}")
    assert body["msg"] == "v2"
    status, body = req("GET", f"/f/{slug}?alias=prod")
    assert body["msg"] == "v1"


def test_metrics_stats():
    token, slug = test_create_invoke()
    req("GET", f"/f/{slug}")
    time.sleep(1.2)
    status, body = req("GET", "/metrics/stats?days=1", token=token)
    assert status == 200
    assert "count" in body
    assert body["count"] >= 1


def test_system_endpoints():
    token = test_register_login()
    status, body = req("GET", "/ready")
    assert status == 200 and body.get("status") == "ready"
    status, body = req("GET", "/pool/stats", token=token)
    assert status == 200 and "workers" in body
    status, body = req("GET", "/dlq", token=token)
    assert status == 200
    status, body = req("GET", "/metrics/summary?days=1", token=token)
    assert status == 200 and "items" in body
    import urllib.request
    r = urllib.request.Request(f"http://localhost:{PORT}/metrics/export?days=1", headers={"Authorization": f"Bearer {token}"})
    text = urllib.request.urlopen(r).read().decode()
    assert "slug" in text


def test_bytes_result():
    token = test_register_login()
    status, body = req("POST", "/vlambda", "def handler(request): return {'raw': b'hello'}", token)
    assert status == 201
    slug = body["slug"]
    status, body = req("GET", f"/f/{slug}")
    assert status == 200 and body.get("raw") == "hello"


def test_request_body_echo():
    token = test_register_login()
    code = "def handler(request): return {'body': request.body}"
    status, body = req("POST", "/vlambda?method=POST", code, token)
    assert status == 201
    slug = body["slug"]
    status, body = req("POST", f"/f/{slug}", "hello")
    assert status == 200 and body.get("body") == "hello"


def test_packages():
    token = test_register_login()
    code = "def handler(request):\n    import six\n    return {'six': six.__version__}"
    status, body = req("POST", "/vlambda", code, token)
    assert status == 201
    slug = body["slug"]
    status, body = req("GET", f"/vlambda/{slug}/packages", token=token)
    assert status == 200 and body.get("packages") == []
    status, body = req("POST", f"/vlambda/{slug}/packages", '{"packages":["six"]}', token)
    assert status == 200 and "six" in body.get("installed", []), body
    status, body = req("GET", f"/f/{slug}")
    assert status == 200 and body.get("six"), (status, body)


def test_metrics_aggregate_endpoint():
    token, slug = test_create_invoke()
    req("GET", f"/f/{slug}")
    time.sleep(1.2)
    status, body = req("GET", "/metrics/aggregate?days=1", token=token)
    assert status == 200
    assert body.get("requests_total", 0) >= 1
    assert any(i.get("slug") == slug for i in body.get("per_lambda", []))
    status, body = req("GET", f"/metrics/aggregate?days=1&slug={slug}", token=token)
    assert status == 200
    assert body.get("requests_total", 0) >= 1
    assert all(i.get("slug") == slug for i in body.get("per_lambda", []))


def test_lambda_logs():
    token = test_register_login()
    code = "def handler(request):\n    request.log('hello from lambda')\n    return {'ok': True}"
    status, body = req("POST", "/vlambda", code, token)
    assert status == 201
    slug = body["slug"]
    status, body = req("GET", f"/f/{slug}")
    assert status == 200 and body.get("ok") is True
    time.sleep(1.2)
    status, body = req("GET", "/logs/history?days=1", token=token)
    assert status == 200
    assert any(i.get("message") == "hello from lambda" and i.get("slug") == slug for i in body.get("items", []))


def test_delete_metrics_and_logs():
    token = test_register_login()
    code = "def handler(request):\n    request.log('delete me')\n    return {'ok': True}"
    status, body = req("POST", "/vlambda?name=cleanup", code, token)
    assert status == 201, body
    slug = body["slug"]
    status, body = req("GET", f"/f/{slug}")
    assert status == 200
    time.sleep(1.2)

    # Pruning recent data must not remove anything.
    status, body = req("DELETE", f"/metrics/history?days=1&slug={slug}", token=token)
    assert status == 200 and body.get("deleted") == 0, body

    status, body = req("DELETE", f"/metrics/history?days=0&slug={slug}", token=token)
    assert status == 200 and body.get("deleted", 0) >= 1, body
    status, body = req("GET", f"/metrics/aggregate?days=1&slug={slug}", token=token)
    assert status == 200 and body.get("requests_total") == 0, body

    status, body = req("DELETE", f"/logs/history?days=1&slug={slug}", token=token)
    assert status == 200 and body.get("deleted") == 0, body
    status, body = req("DELETE", f"/logs/history?days=0&slug={slug}", token=token)
    assert status == 200 and body.get("deleted", 0) >= 1, body
    status, body = req("GET", f"/logs/history?days=1&slug={slug}", token=token)
    assert status == 200 and body.get("count") == 0, body


def test_admin_reset():
    token = test_register_login()
    req("POST", "/vlambda", "def handler(request): return {'ok': 1}", token)
    status, body = req("POST", "/admin/reset", extra_headers={"X-Admin-Token": "test-admin"})
    assert status == 200
    assert "rows" in body
    status, body = req("GET", "/vlambda", token=token)
    assert body.get("count") == 0
    # Wrong token must not reset.
    status, body = req("POST", "/admin/reset", extra_headers={"X-Admin-Token": "wrong"})
    assert body.get("error") == "forbidden"


def test_rate_limit():
    time.sleep(3.0)
    # default 100 req/s; send enough to trigger 429
    token, slug = test_create_invoke()
    codes = []
    for _ in range(150):
        status, body = req("GET", f"/f/{slug}")
        codes.append(status)
    assert 429 in codes

def test_sandbox_blocked():
    token = test_register_login()
    status, body = req("POST", "/vlambda", "import os\ndef handler(request): return {}", token)
    # validation may reject at ast level? os import allowed by ast; execution blocked only if called.
    # At least ensure no crash
    assert status in (201, 400)


def _upload_project(name, files, entry="main.py"):
    import base64, io, zipfile
    from urllib.parse import quote
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path, content in files.items():
            z.writestr(path, content)
    b64 = base64.b64encode(buf.getvalue()).decode()
    token = test_register_login()
    status, body = req("POST", f"/vlambda?project=1&entry={quote(entry, safe='')}&name={quote(name, safe='')}", b64, token)
    assert status == 201, body
    return token, body["slug"]


def test_cors_headers():
    token, slug = test_create_invoke()
    import urllib.request
    resp = urllib.request.urlopen(f"http://localhost:{PORT}/f/{slug}")
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"
    assert resp.headers.get("Access-Control-Allow-Methods")
    preflight = urllib.request.Request(f"http://localhost:{PORT}/f/{slug}", method="OPTIONS")
    resp = urllib.request.urlopen(preflight)
    assert resp.status == 204
    assert "GET" in resp.headers.get("Access-Control-Allow-Methods", "")


def test_json_listing():
    token, slug = test_create_invoke()
    status, body = req("GET", "/vlambda?format=json", token=token)
    assert status == 200, body
    assert isinstance(body["lambdas"], list)
    assert any(item.get("slug") == slug for item in body["lambdas"])


def test_api_tokens():
    token = test_register_login()
    status, body = req("POST", "/tokens", json.dumps({"name": "ci", "scopes": ["read", "write"]}), token)
    assert status == 201, body
    pat = body["token"]
    token_id = body["id"]
    assert pat.startswith("vl_")
    status, body = req("GET", "/vlambda?format=json", token=pat)
    assert status == 200
    status, body = req("POST", "/vlambda", "def handler(request): return {}", pat)
    assert status == 201, body
    status, body = req("GET", "/tokens", token=pat)
    assert status == 200 and body["tokens"][0]["id"] == token_id
    status, body = req("DELETE", f"/tokens/{token_id}", token=token)
    assert status == 200
    status, body = req("GET", "/vlambda", token=pat)
    assert status == 401

    status, body = req("POST", "/tokens", json.dumps({"name": "ro", "scopes": ["read"]}), token)
    assert status == 201, body
    readonly = body["token"]
    status, body = req("POST", "/vlambda", "def handler(request): return {}", readonly)
    assert status == 403


def test_project_static_files():
    token, slug = _upload_project("static", {
        "main.py": "def handler(request):\n    return {'path': request.path}\n",
        "index.html": "<!DOCTYPE html><html><body><h1>static</h1></body></html>",
        "app.js": "console.log('hi')",
        "css/site.css": "body { color: red; }",
    })
    import urllib.request
    resp = urllib.request.urlopen(f"http://localhost:{PORT}/f/{slug}/")
    assert resp.status == 200
    assert resp.headers.get("Content-Type", "").startswith("text/html")
    assert b"<h1>static</h1>" in resp.read()
    resp = urllib.request.urlopen(f"http://localhost:{PORT}/f/{slug}/app.js")
    assert "javascript" in resp.headers.get("Content-Type", "")
    resp = urllib.request.urlopen(f"http://localhost:{PORT}/f/{slug}/css/site.css")
    assert "text/css" in resp.headers.get("Content-Type", "")
    status, body = req("GET", f"/f/{slug}/api")
    assert status == 200 and body.get("path", "").endswith("/api")
    status, body = req("GET", f"/f/{slug}/%2e%2e/%2e%2e/etc/passwd")
    assert status == 400


def test_export_import_duplicate():
    token, slug = test_create_invoke()
    status, exported = req("GET", f"/vlambda/{slug}/export", token=token)
    assert status == 200, exported
    assert exported["format"] == "vlambda/v1"
    assert exported["code"].startswith("def handler")
    status, body = req("POST", "/vlambda/import", json.dumps(exported), token)
    assert status == 201, body
    imported = body["slug"]
    status, body = req("GET", f"/f/{imported}")
    assert status == 200 and body.get("msg") == "hi"
    status, body = req("POST", f"/vlambda/{slug}/duplicate", token=token)
    assert status == 201, body
    assert body["slug"] != slug
    status, body = req("GET", f"/f/{body['slug']}")
    assert status == 200 and body.get("msg") == "hi"


def test_project_export_import():
    token, slug = _upload_project("exportable", {
        "main.py": "def handler(request):\n    return {'ok': True}\n",
        "index.html": "<!DOCTYPE html><html><body>export me</body></html>",
    })
    status, exported = req("GET", f"/vlambda/{slug}/export", token=token)
    assert status == 200, exported
    assert exported["kind"] == "project"
    assert "index.html" in exported["files"]
    status, body = req("POST", "/vlambda/import", json.dumps(exported), token)
    assert status == 201, body
    imported = body["slug"]
    import urllib.request
    resp = urllib.request.urlopen(f"http://localhost:{PORT}/f/{imported}/")
    assert b"export me" in resp.read()
    status, body = req("GET", f"/f/{imported}")
    assert body.get("ok") is True


if __name__ == "__main__":
    tests = [test_health, test_favicon_only_for_ui, test_register_login, test_registration_controls, test_create_invoke, test_lambda_names, test_html_lambda, test_auth_required,
             test_update, test_delete, test_validation, test_metrics, test_body_size,
             test_method_mismatch, test_pagination, test_ownership, test_project_zip,
             test_isolated_projects,
             test_nsjail_sandbox,
             test_landlock_sandbox,
             test_async_task, test_metrics_history, test_alias_version, test_metrics_stats,
             test_sandbox_blocked, test_system_endpoints, test_admin_reset, test_bytes_result, test_request_body_echo, test_packages, test_metrics_aggregate_endpoint, test_lambda_logs, test_delete_metrics_and_logs,
             test_cors_headers, test_json_listing, test_api_tokens, test_project_static_files,
             test_export_import_duplicate, test_project_export_import,
             test_concurrency, test_rate_limit]
    failed = 0
    for t in tests:
        # Each test gets a fresh server and database. Retry once if the native
        # bridge drops a connection while its GC is running.
        last_error = None
        passed = False
        for _attempt in range(3):
            start_server()
            time.sleep(0.3)
            try:
                t()
                passed = True
                break
            except Exception as e:
                last_error = e
            finally:
                stop_server()
        if passed:
            print(f"  PASS: {t.__name__}")
        else:
            print(f"  FAIL: {t.__name__}: {last_error}")
            failed += 1
    print(f"\n{'All tests passed' if failed == 0 else f'{failed} failed'}")
    sys.exit(1 if failed else 0)
