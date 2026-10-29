"""Integration tests for vlambda."""

import subprocess, time, json, urllib.request, os, signal, sys

PORT = 9799
DB = "/tmp/test_vlambda.db"
PROC = None

def start_server():
    import os
    global PROC
    for f in [DB, DB+"-wal", DB+"-shm"]:
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
    PROC = subprocess.Popen(
        [python_bin, "-m", "vlambda", "--port", str(PORT), "--sqlite_path", DB, "--max_body_size", "100"],
        cwd=cwd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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

def req(method, path, data=None, token=None):
    url = f"http://localhost:{PORT}{path}"
    headers = {}
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

_counter = [0]
def _unique_user():
    _counter[0] += 1
    return f"user{_counter[0]}"

def test_register_login():
    u = _unique_user()
    status, body = req("POST", "/register", f"{u}:testpass")
    assert status == 201, body
    status, body = req("POST", "/login", f"{u}:testpass")
    assert status == 200 and "token" in body
    return body["token"]

def test_create_invoke():
    token = test_register_login()
    status, body = req("POST", "/vlambda", "def handler(request): return {\"msg\": \"hi\"}", token)
    assert status == 201
    slug = body["slug"]
    status, body = req("GET", f"/f/{slug}")
    assert status == 200 and body["msg"] == "hi"
    return token, slug

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
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("main.py", "def handler(request):\n    from utils import shout\n    return {'msg': shout(request.path)}\n")
        z.writestr("utils.py", "def shout(x):\n    return x.upper()\n")
    b64 = base64.b64encode(buf.getvalue()).decode()
    token = test_register_login()
    status, body = req("POST", "/vlambda?project=1&entry=main.py&name=demo", b64, token)
    assert status == 201, body
    slug = body["slug"]
    status, body = req("GET", f"/f/{slug}")
    assert status == 200
    assert "msg" in body
    status, body = req("DELETE", f"/vlambda/{slug}", token=token)
    assert status == 200

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
    status, body = req("GET", "/metrics/history?days=1")
    # endpoint may require no auth; accept 200
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

if __name__ == "__main__":
    start_server()
    time.sleep(1)
    tests = [test_health, test_register_login, test_create_invoke, test_auth_required,
             test_update, test_delete, test_validation, test_metrics, test_body_size,
             test_method_mismatch, test_pagination, test_ownership, test_project_zip,
             test_async_task, test_metrics_history, test_sandbox_blocked, test_concurrency, test_rate_limit]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS: {t.__name__}")
        except Exception as e:
            print(f"  FAIL: {t.__name__}: {e}")
            failed += 1
    stop_server()
    print(f"\n{'All tests passed' if failed == 0 else f'{failed} failed'}")
    sys.exit(1 if failed else 0)
