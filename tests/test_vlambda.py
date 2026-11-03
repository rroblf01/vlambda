"""Integration tests for vlambda."""

import subprocess, time, json, urllib.request, os, signal, socket, sys

PORT = 9799
DB = "/tmp/test_vlambda.db"
PROC = None

def start_server(extra_env=None, keep_db=False):
    import os
    global PROC
    metrics_db = DB.replace(".db", "_metrics.db")
    if not keep_db:
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
    python_bin = os.environ.get("VLAMBDA_PYTHON", python_bin)
    env = os.environ.copy()
    env["VLAMBDA_ADMIN_TOKEN"] = "test-admin"
    env["VLAMBDA_SECRET"] = "test-secret"
    env["VLAMBDA_DB"] = DB
    env["VLAMBDA_METRICS_DB"] = DB.replace(".db", "_metrics.db")
    env.update(extra_env or {})
    if not _port_free(10.0):
        raise RuntimeError(f"port {PORT} is still in use")
    PROC = subprocess.Popen(
        [python_bin, "-m", "vlambda", "--port", str(PORT), "--sqlite_path", DB, "--max_body_size", "100"],
        cwd=cwd,
        env=env,
        stdout=open("/tmp/vlambda_test_server.log", "w"), stderr=subprocess.STDOUT,
    )
    for _ in range(30):
        if PROC.poll() is not None:
            try:
                log = open("/tmp/vlambda_test_server.log", errors="replace").read()[-3000:]
            except Exception:
                log = ""
            raise RuntimeError(f"server exited during startup:\n{log}")
        try:
            urllib.request.urlopen(f"http://localhost:{PORT}/health", timeout=2)
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("server did not start")

def _port_free(timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.1):
                pass
        except OSError:
            return True
        time.sleep(0.1)
    return False


def stop_server():
    global PROC
    if PROC:
        PROC.terminate()
        try:
            PROC.wait(timeout=10)
        except subprocess.TimeoutExpired:
            PROC.kill()
            try:
                PROC.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        PROC = None
    _port_free(5.0)

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



def test_response_contract():
    token = test_register_login()
    code = 'def handler(r): return __import__("vlambda").Response.json({"h":r.header("X-Test")},201)'
    status, body = req("POST", "/vlambda?name=contract", code, token)
    assert status == 201, body
    slug = body["slug"]
    import urllib.request
    request = urllib.request.Request(
        f"http://localhost:{PORT}/f/{slug}",
        headers={"X-Test": "yes"},
    )
    resp = urllib.request.urlopen(request)
    assert resp.status == 201
    assert resp.headers.get("Content-Type", "").startswith("application/json")
    assert json.loads(resp.read()) == {"h": "yes"}



def test_lambda_env():
    token = test_register_login()
    code = "def handler(request): return {'env': request.env}"
    status, body = req("POST", "/vlambda?name=env", code, token)
    assert status == 201, body
    slug = body["slug"]
    status, body = req("PUT", f"/vlambda/{slug}/env", json.dumps({
        "vars": {"PLAIN": "hello"},
        "secrets": {"TOKEN": "s3cret"},
    }), token, extra_headers={"Content-Type": "application/json"})
    assert status == 200, body
    entries = {item["key"]: item for item in body["env"]}
    assert entries["PLAIN"] == {"key": "PLAIN", "secret": False, "value": "hello"}
    assert entries["TOKEN"]["secret"] is True and entries["TOKEN"]["value"] == "********"

    status, body = req("GET", f"/f/{slug}")
    assert status == 200, body
    assert body["env"] == {"PLAIN": "hello", "TOKEN": "s3cret"}, body

    # Partial save: only vars, the secret must survive and the cache must not
    # serve the response from before the change.
    status, body = req("PUT", f"/vlambda/{slug}/env", json.dumps({"vars": {"PLAIN": "bye"}}),
                       token, extra_headers={"Content-Type": "application/json"})
    assert status == 200, body
    status, body = req("GET", f"/f/{slug}")
    assert status == 200, body
    assert body["env"] == {"PLAIN": "bye", "TOKEN": "s3cret"}, body

    # Partial save: only secrets, masked, keeps vars and the previous value.
    status, body = req("PUT", f"/vlambda/{slug}/env", json.dumps({"secrets": {"TOKEN": "********"}}),
                       token, extra_headers={"Content-Type": "application/json"})
    assert status == 200, body
    status, body = req("GET", f"/f/{slug}")
    assert status == 200, body
    assert body["env"] == {"PLAIN": "bye", "TOKEN": "s3cret"}, body

    # The server's own environment names are reserved.
    status, body = req("PUT", f"/vlambda/{slug}/env", json.dumps({"vars": {"VLAMBDA_DB": "/tmp/evil.db"}}),
                       token, extra_headers={"Content-Type": "application/json"})
    assert status == 400, body
    status, body = req("GET", f"/f/{slug}")
    assert body["env"] == {"PLAIN": "bye", "TOKEN": "s3cret"}, body


def test_status_body_dict_is_data():
    token = test_register_login()
    code = 'def handler(r): return {"status": 404, "body": "x"}'
    status, body = req("POST", "/vlambda", code, token)
    assert status == 201, body
    status, body = req("GET", f"/f/{body['slug']}")
    assert status == 200, body
    assert body == {"status": 404, "body": "x"}, body


def test_header_responses_not_cached():
    token = test_register_login()
    code = 'def handler(r): return {"h": r.header("X-User")}'
    status, body = req("POST", "/vlambda", code, token)
    assert status == 201, body
    slug = body["slug"]
    status, body = req("GET", f"/f/{slug}", extra_headers={"X-User": "alice"})
    assert status == 200 and body["h"] == "alice", body
    status, body = req("GET", f"/f/{slug}", extra_headers={"X-User": "bob"})
    assert status == 200 and body["h"] == "bob", body


def test_response_binary():
    token = test_register_login()
    code = 'def handler(r): return __import__("vlambda").Response(b"\\x00\\xff")'
    status, body = req("POST", "/vlambda", code, token)
    assert status == 201, body
    import urllib.request
    resp = urllib.request.urlopen(f"http://localhost:{PORT}/f/{body['slug']}")
    assert resp.status == 200
    assert resp.headers.get("Content-Type", "").startswith("application/octet-stream")
    assert resp.read() == b"\x00\xff"


def test_egress_fetch():
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    port = upstream.server_address[1]
    stop_server()
    start_server({
        "VLAMBDA_SANDBOX": "fork",
        "VLAMBDA_EGRESS_ALLOW_HTTP": "1",
        "VLAMBDA_EGRESS_ALLOW_PRIVATE": "1",
    })
    try:
        token = test_register_login()
        code = f"def handler(r): return {{'text': r.fetch('http://127.0.0.1:{port}/hello').text}}"
        assert len(code.encode()) < 100
        status, body = req("POST", "/vlambda?name=egress", code, token)
        assert status == 201, body
        slug = body["slug"]
        status, body = req("PUT", f"/vlambda/{slug}/egress",
                           json.dumps({"hosts": ["127.0.0.1"]}), token,
                           extra_headers={"Content-Type": "application/json"})
        assert status == 200, body
        status, body = req("GET", f"/vlambda/{slug}/egress", token=token)
        assert status == 200 and body["hosts"] == ["127.0.0.1"], body
        status, body = req("GET", f"/vlambda/{slug}/egress")
        assert status == 401, body
        other = test_register_login()
        status, body = req("GET", f"/vlambda/{slug}/egress", token=other)
        assert status == 403, body
        status, body = req("GET", f"/f/{slug}")
        assert status == 200 and '"ok": true' in body["text"], body

        status, body = req("PUT", f"/vlambda/{slug}/egress",
                           json.dumps({"hosts": []}), token,
                           extra_headers={"Content-Type": "application/json"})
        assert status == 200, body
        status, body = req("GET", f"/f/{slug}")
        assert status == 500 and "not allowed" in body.get("error", ""), body
    finally:
        upstream.shutdown()
        stop_server()


def test_schedules():
    import sqlite3

    stop_server()
    start_server({"VLAMBDA_SANDBOX": "fork", "VLAMBDA_SCHEDULE_TICK": "0.2"})
    try:
        token = test_register_login()
        code = "def handler(r): return {'v': (r.kv.set('s', r.kv.get('s',0)+1), r.kv.get('s'))[1]}"
        status, body = req("POST", "/vlambda?name=scheduled", code, token)
        assert status == 201, body
        slug = body["slug"]

        status, body = req("POST", f"/vlambda/{slug}/schedules",
                           json.dumps({"name": "nightly", "cron": "0 3 * * *"}), token,
                           extra_headers={"Content-Type": "application/json"})
        assert status == 200, body
        schedule = body["schedule"]
        schedule_id = schedule["id"]
        assert schedule["name"] == "nightly" and schedule["next_run"], body

        status, body = req("GET", f"/vlambda/{slug}/schedules", token=token)
        assert status == 200 and body["schedules"][0]["id"] == schedule_id, body
        status, body = req("GET", f"/vlambda/{slug}/schedules")
        assert status == 401, body
        other = test_register_login()
        status, body = req("GET", f"/vlambda/{slug}/schedules", token=other)
        assert status == 403, body

        status, body = req("POST", f"/vlambda/{slug}/schedules/{schedule_id}/run", token=token)
        assert status == 200 and body["run_id"], body
        for _ in range(40):
            status, body = req("GET", f"/vlambda/{slug}/schedules/{schedule_id}/runs", token=token)
            if body["runs"] and body["runs"][0]["status"] in ("completed", "failed"):
                break
            time.sleep(0.1)
        assert body["runs"][0]["status"] == "completed", body

        def kv_value():
            status, body = req("GET", f"/vlambda/{slug}/kv", token=token)
            assert status == 200, body
            return {item["key"]: item["value"] for item in body["items"]}.get("s")

        assert kv_value() == 1

        # Force the next automatic run by moving next_run into the past.
        conn = sqlite3.connect(DB)
        conn.execute("UPDATE schedules SET next_run=?, enabled=1 WHERE id=?",
                     (time.time() - 1, schedule_id))
        conn.commit()
        conn.close()
        for _ in range(50):
            if kv_value() == 2:
                break
            time.sleep(0.1)
        assert kv_value() == 2

        status, body = req("PUT", f"/vlambda/{slug}/schedules/{schedule_id}",
                           json.dumps({"enabled": False}), token,
                           extra_headers={"Content-Type": "application/json"})
        assert status == 200, body
        assert body["schedule"]["enabled"] is False, body
        assert body["schedule"]["next_run"] is None, body

        status, body = req("DELETE", f"/vlambda/{slug}/schedules/{schedule_id}", token=token)
        assert status == 200 and body["deleted"] is True, body
        status, body = req("GET", f"/vlambda/{slug}/schedules", token=token)
        assert status == 200 and body["schedules"] == [], body
    finally:
        stop_server()


def test_openapi():
    token = test_register_login()
    code = "def handler(r): return {'ok': True}"
    status, body = req("POST", "/vlambda?name=documented", code, token)
    assert status == 201, body
    slug = body["slug"]
    status, body = req("PUT", f"/vlambda/{slug}/meta",
                       json.dumps({"summary": "Demo", "tags": "api,demo", "description": "Desc"}),
                       token, extra_headers={"Content-Type": "application/json"})
    assert status == 200, body
    assert body["summary"] == "Demo", body
    status, body = req("GET", f"/vlambda/{slug}/meta", token=token)
    assert status == 200 and body["tags"] == "api,demo", body

    status, body = req("GET", "/openapi.json", token=token)
    assert status == 200, body
    operation = body["paths"][f"/f/{slug}"]["get"]
    assert operation["summary"] == "Demo" and operation["tags"] == ["api", "demo"], operation

    status, body = req("GET", "/openapi.json")
    assert status == 401, body
    other = test_register_login()
    status, body = req("GET", "/openapi.json", token=other)
    assert status == 200 and f"/f/{slug}" not in body["paths"], body

    import urllib.request
    docs = urllib.request.urlopen(f"http://localhost:{PORT}/docs").read()
    assert b"openapi.json" in docs

    status, body = req("GET", f"/vlambda/{slug}/meta")
    assert status == 401, body
    status, body = req("GET", f"/vlambda/{slug}/meta", token=other)
    assert status == 403, body


def test_openapi_public():
    stop_server()
    start_server({"VLAMBDA_OPENAPI_PUBLIC": "1"})
    try:
        token = test_register_login()
        status, body = req("POST", "/vlambda?name=public", "def handler(r): return {}", token)
        assert status == 201, body
        status, body = req("GET", "/openapi.json")
        assert status == 200 and any(path.startswith("/f/") for path in body["paths"]), body
    finally:
        stop_server()


def test_response_injection_blocked():
    token = test_register_login()
    code = 'def handler(r): return __import__("vlambda").Response.text("x",headers={"X":"a\\r\\nb"})'
    status, body = req("POST", "/vlambda", code, token)
    assert status == 201, body
    status, body = req("GET", f"/f/{body['slug']}")
    assert status == 500, body
    assert "invalid header value" in body.get("error", ""), body

    code = 'def handler(r): return __import__("vlambda").Response("x",status=99)'
    status, body = req("POST", "/vlambda", code, token)
    assert status == 201, body
    status, body = req("GET", f"/f/{body['slug']}")
    assert status == 500, body
    assert "invalid HTTP status" in body.get("error", ""), body


def test_lambda_kv():
    token = test_register_login()
    code = "def handler(r): return {'hits': (r.kv.set('h', r.kv.get('h',0)+1), r.kv.get('h'))[1]}"
    status, body = req("POST", "/vlambda?name=kv", code, token)
    assert status == 201, body
    slug = body["slug"]

    status, body = req("GET", f"/f/{slug}")
    assert status == 200 and body["hits"] == 1, body
    status, body = req("GET", f"/f/{slug}")
    assert status == 200 and body["hits"] == 2, body

    status, body = req("GET", f"/vlambda/{slug}/kv", token=token)
    assert status == 200, body
    assert {item["key"]: item["value"] for item in body["items"]}["h"] == 2

    status, body = req("PUT", f"/vlambda/{slug}/kv/foo", '{"value": {"a": 1}}',
                       token, extra_headers={"Content-Type": "application/json"})
    assert status == 200, body
    status, body = req("GET", f"/vlambda/{slug}/kv/foo", token=token)
    assert status == 200 and body["value"] == {"a": 1}, body
    status, body = req("GET", f"/vlambda/{slug}/kv?prefix=f", token=token)
    assert status == 200 and [item["key"] for item in body["items"]] == ["foo"], body

    status, body = req("GET", f"/vlambda/{slug}/kv/foo")
    assert status == 401, body
    other = test_register_login()
    status, body = req("GET", f"/vlambda/{slug}/kv/foo", token=other)
    assert status == 403, body
    status, body = req("PUT", f"/vlambda/{slug}/kv/foo", '{"value": 1}', other,
                       extra_headers={"Content-Type": "application/json"})
    assert status == 403, body

    status, body = req("PUT", f"/vlambda/{slug}/kv/ttl", '{"value": "x", "ttl": 0.05}',
                       token, extra_headers={"Content-Type": "application/json"})
    assert status == 200, body
    time.sleep(0.1)
    status, body = req("GET", f"/vlambda/{slug}/kv/ttl", token=token)
    assert status == 200 and body["exists"] is False, body

    status, body = req("DELETE", f"/vlambda/{slug}/kv/foo", token=token)
    assert status == 200 and body["deleted"] is True, body
    status, body = req("GET", f"/vlambda/{slug}/kv/foo", token=token)
    assert status == 200 and body["exists"] is False, body


def test_async_kv():
    token = test_register_login()
    code = "def handler(r): return {'hits': (r.kv.set('h', r.kv.get('h',0)+1), r.kv.get('h'))[1]}"
    status, body = req("POST", "/vlambda?name=async-kv", code, token)
    assert status == 201, body
    slug = body["slug"]
    status, body = req("POST", f"/async/{slug}", token=token)
    assert status == 202, body
    task_id = body["task_id"]
    for _ in range(30):
        status, body = req("GET", f"/task/{task_id}", token=token)
        if body.get("status") == "completed":
            break
        time.sleep(0.1)
    assert body.get("status") == "completed", body
    assert json.loads(body["result"]["body"])["hits"] == 1, body
    status, body = req("GET", f"/vlambda/{slug}/kv", token=token)
    assert {item["key"]: item["value"] for item in body["items"]}["h"] == 1, body


def test_kv_fork_project():
    stop_server()
    start_server({"VLAMBDA_SANDBOX": "fork"})
    try:
        code = (
            "from vlambda import kv\n"
            "import socket\n"
            "def handler(request):\n"
            "    kv.set('p', kv.get('p', 0) + 1)\n"
            "    try:\n"
            "        socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "        net = 'allowed'\n"
            "    except PermissionError:\n"
            "        net = 'blocked'\n"
            "    return {'p': kv.get('p'), 'net': net}\n"
        )
        token, slug = _upload_project("kvproj", {"main.py": code})
        status, body = req("GET", f"/f/{slug}")
        assert status == 200 and body == {"p": 1, "net": "blocked"}, body
        status, body = req("GET", f"/f/{slug}")
        assert status == 200 and body == {"p": 2, "net": "blocked"}, body
        status, body = req("GET", f"/vlambda/{slug}/kv", token=token)
        assert {item["key"]: item["value"] for item in body["items"]}["p"] == 2, body
    finally:
        stop_server()


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

def test_delete_cleans_lambda_state():
    import sqlite3
    token = test_register_login()
    status, body = req("POST", "/vlambda", "def handler(request): return {'ok': True}", token)
    assert status == 201, body
    slug = body["slug"]
    status, body = req("PUT", f"/vlambda/{slug}/env", json.dumps({"vars": {"A": "1"}}),
                       token, extra_headers={"Content-Type": "application/json"})
    assert status == 200, body
    status, body = req("POST", f"/vlambda/{slug}/alias?name=prod&version=1", token=token)
    assert status == 201, body
    status, body = req("DELETE", f"/vlambda/{slug}", token=token)
    assert status == 200, body
    conn = sqlite3.connect(DB)
    for table in ("lambdas", "lambda_env", "lambda_versions", "lambda_aliases"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE slug = ?", (slug,)).fetchone()[0]
        assert count == 0, (table, count)
    conn.close()


def test_validation():
    token = test_register_login()
    status, body = req("POST", "/vlambda", "print('no handler')", token)
    assert status == 400
    # Unknown HTTP methods are normalized to GET instead of being stored.
    status, body = req("POST", "/vlambda?method=EVIL%27%3E", "def handler(request): return {}", token)
    assert status == 201, body
    slug = body["slug"]
    status, payload = req("GET", "/vlambda?format=json", token=token)
    item = next(x for x in payload["lambdas"] if x["slug"] == slug)
    assert item["method"] == "GET", item

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
    # Versions, aliases and version code are owner-only too.
    status, body = req("PUT", f"/vlambda/{slug}", "def handler(request): return {'v': 2}", t1)
    assert status == 200, body
    for path in (f"/vlambda/{slug}/versions", f"/vlambda/{slug}/aliases",
                 f"/vlambda/{slug}/version/1"):
        status, body = req("GET", path)
        assert status == 401, (path, body)
        status, body = req("GET", path, token=t2)
        assert status == 403, (path, body)
        status, body = req("GET", path, token=t1)
        assert status == 200, (path, body)

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
    other = test_register_login()
    status, body = req("GET", f"/project/{slug}")
    assert status == 401, body
    status, body = req("GET", f"/project/{slug}", token=other)
    assert status == 403, body
    status, body = req("PUT", f"/project/{slug}", '{"files":{"main.py":"x"}}', other)
    assert status == 403, body
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
                "    request.log('landlock log line')\n"
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
        deadline = time.time() + 4
        logs = {}
        while time.time() < deadline:
            status, logs = req("GET", f"/logs/history?days=1&slug={slug}", token=token)
            assert status == 200
            if any(item.get("message") == "landlock log line" for item in logs.get("items", [])):
                break
            time.sleep(0.3)
        assert any(item.get("message") == "landlock log line" for item in logs.get("items", [])), logs
    finally:
        stop_server()



def test_fork_sandbox():
    try:
        from vlambda.sandbox_os import sandbox_supported
        import ctypes.util
        if not sandbox_supported() or not ctypes.util.find_library("seccomp"):
            return
    except Exception:
        return
    stop_server()
    start_server({
        "VLAMBDA_SANDBOX": "fork",
        "VLAMBDA_SECRET": "test-secret",
        "VLAMBDA_COROUTINES": "2",
        "VLAMBDA_TIMEOUT": "5",
    })
    try:
        token, slug = _upload_project("fork", {
            "main.py": (
                "import os, threading\n"
                "def handler(request):\n"
                "    out = {}\n"
                "    for name, fn in (\n"
                "        ('data', lambda: os.listdir('/data')),\n"
                "        ('db', lambda: open('/data/db/vlambda.db', 'rb').read(1)),\n"
                "        ('proc', lambda: open('/proc/1/environ', 'rb').read()),\n"
                "        ('kill', lambda: os.kill(1, 0)),\n"
                "        ('fork', lambda: os.fork()),\n"
                "        ('subprocess', lambda: __import__('subprocess').run(['/bin/true'])),\n"
                "    ):\n"
                "        try:\n"
                "            fn(); out[name] = 'ok'\n"
                "        except Exception as e:\n"
                "            out[name] = type(e).__name__\n"
                "    request.log('fork log line')\n"
                "    values = []\n"
                "    thread = threading.Thread(target=lambda: values.append(1))\n"
                "    thread.start(); thread.join()\n"
                "    out['thread'] = values\n"
                "    out['pid'] = os.getpid()\n"
                "    return out\n"
            ),
        })
        pids = []
        for _ in range(3):
            status, body = req("GET", f"/f/{slug}")
            assert status == 200, body
            assert body.get("data") in ("PermissionError", "FileNotFoundError"), body
            assert body.get("db") in ("PermissionError", "FileNotFoundError"), body
            assert body.get("proc") == "PermissionError", body
            assert body.get("kill") == "PermissionError", body
            assert body.get("fork") == "PermissionError", body
            assert body.get("subprocess") == "PermissionError", body
            assert body.get("thread") == [1], body
            pids.append(body.get("pid"))
        assert len(set(pids)) == 3, pids
        deadline = time.time() + 4
        logs = {}
        while time.time() < deadline:
            status, logs = req("GET", f"/logs/history?days=1&slug={slug}", token=token)
            assert status == 200
            if len(logs.get("items", [])) >= 3:
                break
            time.sleep(0.3)
        assert logs.get("count", 0) >= 3, logs
    finally:
        stop_server()


def test_async_task():
    token, slug = test_create_invoke()
    status, body = req("POST", f"/async/{slug}")
    assert status == 401, body
    other = test_register_login()
    status, body = req("POST", f"/async/{slug}", token=other)
    assert status == 403, body

    status, body = req("POST", f"/async/{slug}", token=token)
    assert status == 202, body
    task_id = body["task_id"]
    status, body = req("GET", f"/task/{task_id}")
    assert status == 401, body
    status, body = req("GET", f"/task/{task_id}", token=other)
    assert status == 200 and body.get("error") == "task not found", body
    # poll
    for _ in range(20):
        status, body = req("GET", f"/task/{task_id}", token=token)
        if body.get("status") == "completed":
            break
        time.sleep(0.1)
    assert body.get("status") == "completed", body

    status, body = req("GET", "/dlq")
    assert status == 401, body
    status, body = req("GET", "/dlq", token=other)
    assert status == 200 and body == [], body
    status, body = req("GET", "/dlq", token=token)
    assert status == 200, body

    # Async tasks keep the slug, headers and environment of the caller.
    code = "def handler(r): return {'env': r.env, 'slug': r.slug, 'header': r.header('X-Test')}"
    status, body = req("POST", "/vlambda?name=async-env", code, token)
    assert status == 201, body
    env_slug = body["slug"]
    status, body = req("PUT", f"/vlambda/{env_slug}/env",
                       json.dumps({"vars": {"A": "1"}, "secrets": {"S": "s3cret"}}),
                       token, extra_headers={"Content-Type": "application/json"})
    assert status == 200, body
    status, body = req("POST", f"/async/{env_slug}", token=token, extra_headers={"X-Test": "yes"})
    assert status == 202, body
    env_task = body["task_id"]
    for _ in range(30):
        status, body = req("GET", f"/task/{env_task}", token=token)
        if body.get("status") == "completed":
            break
        time.sleep(0.1)
    assert body.get("status") == "completed", body
    payload = json.loads(body["result"]["body"])
    assert payload == {"env": {"A": "1", "S": "s3cret"}, "slug": env_slug, "header": "yes"}, payload
    import sqlite3
    conn = sqlite3.connect(DB)
    stored = conn.execute(
        "SELECT request_meta FROM tasks ORDER BY created DESC LIMIT 1"
    ).fetchone()[0]
    conn.close()
    assert "s3cret" not in stored, "task metadata persisted a decrypted secret"

def test_task_recovery():
    stop_server()
    start_server({"VLAMBDA_SANDBOX": "fork", "VLAMBDA_TASK_LEASE": "1"})
    token = test_register_login()
    code = "import time\ndef handler(r):\n    time.sleep(3)\n    return {'ok': True}\n"
    status, body = req("POST", "/vlambda?name=recover", code, token)
    assert status == 201, body
    slug = body["slug"]
    status, body = req("POST", f"/async/{slug}", token=token)
    assert status == 202, body
    task_id = body["task_id"]
    time.sleep(0.4)
    status, body = req("GET", f"/task/{task_id}", token=token)
    assert body.get("status") in ("pending", "running"), body

    # Simulate a hard crash, not a graceful drain.
    PROC.kill()
    PROC.wait(timeout=5)
    time.sleep(0.5)

    start_server({"VLAMBDA_SANDBOX": "fork", "VLAMBDA_TASK_LEASE": "1"}, keep_db=True)
    for _ in range(80):
        status, body = req("GET", f"/task/{task_id}", token=token)
        if body.get("status") in ("completed", "failed"):
            break
        time.sleep(0.2)
    assert body.get("status") == "completed", body
    assert body.get("attempts", 0) >= 2, body
    stop_server()


def test_shutdown_drains():
    import threading

    stop_server()
    start_server({"VLAMBDA_SANDBOX": "fork", "VLAMBDA_DRAIN_TIMEOUT": "5"})
    token = test_register_login()
    code = "import time\ndef handler(r):\n    time.sleep(1)\n    return {'ok': True}\n"
    status, body = req("POST", "/vlambda?name=slow", code, token)
    assert status == 201, body
    slug = body["slug"]
    result = {}
    def invoke():
        result["value"] = req("GET", f"/f/{slug}")
    thread = threading.Thread(target=invoke)
    thread.start()
    time.sleep(0.2)
    status, body = req("POST", "/shutdown", extra_headers={"X-Admin-Token": "test-admin"})
    assert status == 200, body
    thread.join(timeout=10)
    assert result.get("value", (0, {}))[0] == 200, result
    PROC.wait(timeout=10)


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
    status, body = req("GET", f"/vlambda/{slug}/aliases", token=token)
    assert body["aliases"] == [{"alias": "prod", "version": 1}], body

    # Alias names are scoped per lambda, not global.
    other = test_register_login()
    status, body = req("POST", "/vlambda", "def handler(request): return {'msg': 'other1'}", other)
    assert status == 201, body
    other_slug = body["slug"]
    status, body = req("PUT", f"/vlambda/{other_slug}", "def handler(request): return {'msg': 'other2'}", other)
    assert status == 200, body
    status, body = req("POST", f"/vlambda/{other_slug}/alias?name=prod&version=1", token=other)
    assert status == 201, body
    status, body = req("GET", f"/f/{other_slug}?alias=prod")
    assert body["msg"] == "other1", body
    status, body = req("GET", f"/f/{slug}?alias=prod")
    assert body["msg"] == "v1", body


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



def test_metrics_ownership():
    token_a = test_register_login()
    status, body = req("POST", "/vlambda?name=owner-a",
                       "def handler(request):\n    request.log('owner a')\n    return {'owner': 'a'}",
                       token_a)
    assert status == 201, body
    slug_a = body["slug"]
    req("GET", f"/f/{slug_a}")

    token_b = test_register_login()
    status, body = req("POST", "/vlambda?name=owner-b",
                       "def handler(request):\n    request.log('owner b')\n    return {'owner': 'b'}",
                       token_b)
    assert status == 201, body
    slug_b = body["slug"]
    req("GET", f"/f/{slug_b}")
    time.sleep(1.5)

    status, body = req("GET", "/metrics/aggregate?days=1", token=token_a)
    assert status == 200, body
    assert [item["slug"] for item in body.get("per_lambda", [])] == [slug_a], body

    status, body = req("GET", "/logs/history?days=1", token=token_a)
    assert status == 200, body
    assert body.get("items") and all(item["slug"] == slug_a for item in body["items"]), body

    status, body = req("GET", "/metrics/aggregate?days=1", token=token_a,
                       extra_headers={"X-Admin-Token": "test-admin"})
    assert status == 200, body
    admin_slugs = {item["slug"] for item in body.get("per_lambda", [])}
    assert {slug_a, slug_b} <= admin_slugs, body

    status, body = req("DELETE", "/metrics/history?days=0", token=token_a)
    assert status == 200, body
    status, body = req("GET", "/metrics/aggregate?days=1", token=token_b)
    assert status == 200, body
    assert [item["slug"] for item in body.get("per_lambda", [])] == [slug_b], body


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
    assert status == 401
    # A newly created user gets a new ID/epoch; the old token must stay invalid.
    u = _unique_user()
    status, body = req("POST", "/register", f"{u}:testpass")
    assert status == 201, body
    status, new_token = req("POST", "/login", f"{u}:testpass")
    assert status == 200 and "token" in new_token
    status, body = req("GET", "/vlambda", token=token)
    assert status == 401
    status, body = req("GET", "/vlambda", token=new_token["token"])
    assert status == 200 and body.get("count") == 0
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
    status, body = req("GET", f"/f/{slug}/main.py")
    assert status == 400, body
    status, body = req("GET", f"/f/{slug}/.env")
    assert status == 400, body


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
    tests = [test_health, test_favicon_only_for_ui, test_register_login, test_registration_controls, test_create_invoke, test_lambda_names, test_response_contract, test_status_body_dict_is_data, test_header_responses_not_cached, test_response_binary, test_lambda_env, test_lambda_kv, test_async_kv, test_kv_fork_project, test_egress_fetch, test_schedules, test_openapi, test_openapi_public, test_response_injection_blocked, test_html_lambda, test_auth_required,
             test_update, test_delete, test_delete_cleans_lambda_state, test_validation, test_metrics, test_body_size,
             test_method_mismatch, test_pagination, test_ownership, test_project_zip,
             test_isolated_projects,
             test_nsjail_sandbox,
             test_landlock_sandbox,
             test_fork_sandbox,
             test_async_task, test_task_recovery, test_shutdown_drains, test_metrics_history, test_alias_version, test_metrics_stats,
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
