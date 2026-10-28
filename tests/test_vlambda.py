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
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read())
        except: return e.code, {}

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

if __name__ == "__main__":
    start_server()
    time.sleep(1)
    tests = [test_health, test_register_login, test_create_invoke, test_auth_required,
             test_update, test_delete, test_validation, test_metrics, test_body_size]
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
