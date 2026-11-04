import os
import tempfile
import zipfile
import io
import time

import pytest

from vlambda.sandbox import make_sandbox, make_namespace, blocked_import, SAFE_BUILTINS
from vlambda.cache import get as get_compiled, get_response, set_response, invalidate, clear
from vlambda.pool import Pool
from vlambda.metrics import init as metrics_init, record, query, close
from vlambda.project import _safe_extract, _load_handler, delete_project


def test_sandbox_safe_builtin():
    compiled, ns = make_sandbox("def handler(request): return abs(-5)", None)
    exec(compiled, ns)
    assert ns["handler"](None) == 5


def test_sandbox_blocked_module():
    for module in ("os", "subprocess", "posix", "_socket", "ctypes", "sqlite3", "threading"):
        with pytest.raises(ImportError):
            blocked_import(module)


def test_sandbox_compile_error():
    with pytest.raises(SyntaxError):
        make_sandbox("def handler(request) return 1", None)


def test_sandbox_missing_handler():
    compiled, ns = make_sandbox("x = 1", None)
    exec(compiled, ns)
    assert ns.get("handler") is None


def test_cache_basic():
    code = "def handler(request): return 42"
    compiled, ns, handler = get_compiled(code)
    assert handler is not None
    assert handler(None) == 42


def test_cache_lru_eviction(monkeypatch):
    import vlambda.cache as cache
    monkeypatch.setattr(cache, "_MAX_SIZE", 2)
    clear()
    h1 = get_compiled("def handler(request): return 1")[2]
    h2 = get_compiled("def handler(request): return 2")[2]
    h3 = get_compiled("def handler(request): return 3")[2]
    # First should be evicted
    assert get_compiled("def handler(request): return 1")[2] is not h1
    clear()


def test_response_cache_ttl(monkeypatch):
    from vlambda.shared_cache import clear as _clear
    _clear()
    import vlambda.shared_cache as sc
    monkeypatch.setattr(sc, "_TTL", 0)
    set_response("k", "value")
    time.sleep(0.01)
    assert get_response("k") is None


def test_response_cache_eviction(monkeypatch):
    from vlambda.shared_cache import clear as _clear
    _clear()
    import vlambda.shared_cache as sc
    monkeypatch.setattr(sc, "_MAX_ENTRIES", 2)
    monkeypatch.setattr(sc, "_TTL", 10)
    set_response("a", "1")
    set_response("b", "2")
    set_response("c", "3")
    assert get_response("a") is None
    assert get_response("c") == "3"


def test_pool_basic():
    pool = Pool(2)
    code = "def handler(request): return request.path.upper()"
    res = pool.execute(code, "GET", "/abc", "", b"")
    assert res["status"] == 200
    assert res["body"] == "/ABC"
    pool.stop()


def test_runner_env_strips_secrets(monkeypatch):
    from vlambda.pool import _runner_env
    monkeypatch.setenv("VLAMBDA_SECRET", "super-secret")
    monkeypatch.setenv("VLAMBDA_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("VLAMBDA_DB", "/data/db/vlambda.db")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "cloud-secret")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@example/db")
    env = _runner_env()
    assert not any(key.startswith("VLAMBDA_") for key in env)
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "DATABASE_URL" not in env
    assert env["HOME"] == "/tmp"


def test_landlock_read_paths():
    from vlambda.pool import _landlock_read_paths
    paths = _landlock_read_paths()
    assert "/" not in paths
    assert all(not path.startswith("/data") for path in paths)


def test_nsjail_config():
    from vlambda.pool import _nsjail_config
    cfg = _nsjail_config("/data/projects/x", "/data/packages/y")
    assert "clone_newuser: true" in cfg
    assert "clone_newpid: true" in cfg
    assert "clone_newnet: true" in cfg
    assert "iface_no_lo: true" in cfg
    assert "mount_proc: false" in cfg
    assert 'src: "/data/projects/x" dst: "/work"' in cfg
    assert 'src: "/data/packages/y" dst: "/vendor"' in cfg
    assert 'src: "/opt/empty" dst: "/data"' in cfg
    assert 'src: "/opt/empty" dst: "/proc"' in cfg
    assert "uidmap" in cfg and 'inside_id: "65534"' in cfg
    assert "DENY {" in cfg and "mount" in cfg
    assert 'path: "/opt/venv/bin/python"' in cfg


def test_metrics_persistence(tmp_path):
    db = str(tmp_path / "metrics.db")
    metrics_init(db, 1)
    record("req1", "slug1", "GET", "/f/x", 1.5, "")
    close()
    # reopen
    metrics_init(db, 1)
    data = query(1)
    assert data["count"] >= 1
    assert data["items"][0]["slug"] == "slug1"
    close()


def test_safe_extract_rejects_traversal():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.py", "x = 1")
    buf.seek(0)
    zf = zipfile.ZipFile(buf)
    dest = tempfile.mkdtemp()
    with pytest.raises(ValueError):
        _safe_extract(zf, dest)


def test_project_load_handler(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "main.py").write_text("def handler(request): return {'ok': True}\n")
    (proj / "utils.py").write_text("def shout(x): return x.upper()\n")
    handler = _load_handler(str(proj), "main.py")
    from vlambda.request import Request
    assert handler(Request()) == {"ok": True}


def test_delete_project(tmp_path):
    proj = tmp_path / "proj2"
    proj.mkdir()
    (proj / "main.py").write_text("def handler(request): return 1\n")
    delete_project(str(proj))
    assert not proj.exists()


def test_gc_orphans_respects_grace(tmp_path, monkeypatch):
    import sqlite3
    from vlambda import project

    db = tmp_path / "vlambda.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE lambdas (kind TEXT, project_dir TEXT)")
    conn.commit()
    conn.close()

    projects = tmp_path / "projects"
    projects.mkdir()
    fresh = projects / "fresh"
    old = projects / "old"
    fresh.mkdir()
    old.mkdir()
    (fresh / "main.py").write_text("x = 1")
    (old / "main.py").write_text("x = 1")
    stale = time.time() - 3600
    os.utime(old, (stale, stale))

    monkeypatch.setenv("VLAMBDA_DB", str(db))
    monkeypatch.setattr(project, "_PROJECTS_DIR", str(projects))
    removed = project.gc_orphans()
    assert removed == 1
    assert fresh.exists()
    assert not old.exists()


def test_env_encryption_roundtrip(monkeypatch):
    from vlambda.env import encrypt, decrypt
    monkeypatch.setenv("VLAMBDA_SECRET", "test-master")
    token = encrypt("super-secret-value")
    assert "super-secret-value" not in token
    assert decrypt(token) == "super-secret-value"
    with pytest.raises(Exception):
        decrypt(token[:-1] + ("A" if token[-1] != "A" else "B"))


def test_response_header_validation():
    from vlambda.response import Response, serialize

    response = Response.text("ok", headers={"X-Ok": "yes"})
    assert ("X-Ok", "yes") in response.headers
    with pytest.raises(ValueError):
        Response.text("x", headers={"Bad Header": "x"})
    with pytest.raises(ValueError):
        Response.text("x", headers={"X-Bad": "a\r\nInjected: 1"})
    with pytest.raises(ValueError):
        Response(b"x", cookies=["a=b\r\nInjected: 1"])
    with pytest.raises(ValueError):
        Response("x", status=99)
    with pytest.raises(ValueError):
        Response("x", status=600)

    response = Response.text("x")
    response.set_header("Content-Length", "999")
    response.set_header("Connection", "keep-alive")
    assert all(name.lower() not in ("content-length", "connection") for name, _ in response.headers)

    tampered = Response.text("x")
    tampered.headers.append(("X-Bad", "a\nb"))
    with pytest.raises(ValueError):
        serialize(tampered)

    clean = serialize(Response.text("ok", headers={"X-Ok": "yes"}))
    assert {"name": "X-Ok", "value": "yes"} in clean["headers"]
    assert clean["body"] == "ok"


def test_env_partial_update(tmp_path, monkeypatch):
    import json
    import sqlite3
    from vlambda import dbpool
    from vlambda.env import clear_cache, get_env, save_env

    db = tmp_path / "env.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE lambdas (slug TEXT, owner_id INTEGER)")
    conn.execute(
        "CREATE TABLE lambda_env (slug TEXT NOT NULL, key TEXT NOT NULL,"
        " is_secret INTEGER NOT NULL DEFAULT 0, value TEXT NOT NULL,"
        " PRIMARY KEY (slug, key))"
    )
    conn.execute("INSERT INTO lambdas VALUES ('l-env', 7)")
    conn.commit()
    conn.close()

    monkeypatch.setenv("VLAMBDA_DB", str(db))
    monkeypatch.setenv("VLAMBDA_SECRET", "unit-secret")
    clear_cache()
    try:
        body = json.loads(save_env("l-env", "7", json.dumps({
            "vars": {"A": "1"},
            "secrets": {"S": "one"},
        })))
        assert {item["key"]: item["value"] for item in body["env"]} == {"A": "1", "S": "********"}
        assert get_env("l-env") == {"A": "1", "S": "one"}

        # Only vars: the secret must survive.
        save_env("l-env", "7", json.dumps({"vars": {"A": "2"}}))
        assert get_env("l-env") == {"A": "2", "S": "one"}

        # Only secrets, masked: vars survive and the secret is unchanged.
        save_env("l-env", "7", json.dumps({"secrets": {"S": "********"}}))
        assert get_env("l-env") == {"A": "2", "S": "one"}

        # Only a new secret replaces the secret set; vars survive.
        save_env("l-env", "7", json.dumps({"secrets": {"T": "two"}}))
        assert get_env("l-env") == {"A": "2", "T": "two"}

        # A mask for an unknown secret must not be stored literally.
        save_env("l-env", "7", json.dumps({"secrets": {"N": "********"}}))
        assert "N" not in get_env("l-env")

        # Names used by the runner/server itself are rejected.
        body = json.loads(save_env("l-env", "7", json.dumps({"vars": {"VLAMBDA_DB": "/tmp/x"}})))
        assert "error" in body
        assert get_env("l-env")["A"] == "2"
    finally:
        clear_cache()
        dbpool.close_all()


def test_kv_store_roundtrip(tmp_path, monkeypatch):
    import json
    import sqlite3
    from vlambda import dbpool, kv_store

    db = tmp_path / "kv.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE lambdas (slug TEXT, owner_id INTEGER)")
    conn.execute(
        "CREATE TABLE lambda_kv (slug TEXT NOT NULL, key TEXT NOT NULL,"
        " value TEXT NOT NULL, expires_at REAL, updated_at REAL NOT NULL,"
        " PRIMARY KEY (slug, key))"
    )
    conn.execute("INSERT INTO lambdas VALUES ('l1', 1)")
    conn.execute("INSERT INTO lambdas VALUES ('l2', 2)")
    conn.commit()
    conn.close()

    monkeypatch.setenv("VLAMBDA_DB", str(db))
    dbpool.close_all()
    dbpool.init(str(db))
    try:
        kv_store.set_value("l1", "text", "hello")
        assert kv_store.get_value("l1", "text")[:2] == (True, "hello")
        kv_store.set_value("l1", "obj", {"a": [1, 2]})
        assert kv_store.get_value("l1", "obj")[1] == {"a": [1, 2]}
        kv_store.set_value("l1", "bin", b"\x00\xff")
        assert kv_store.get_value("l1", "bin")[1] == b"\x00\xff"
        kv_store.set_value("l1", "number", 1)
        assert kv_store.incr_value("l1", "number", 4)["value"] == 5
        kv_store.set_value("l1", "ttl", "soon", ttl=0.05)
        time.sleep(0.1)
        assert kv_store.get_value("l1", "ttl")[0] is False
        assert {item["key"] for item in kv_store.list_values("l1")} >= {"text", "obj", "bin", "number"}
        assert kv_store.delete_value("l1", "text") is True
        assert kv_store.delete_value("l1", "text") is False
        assert kv_store.clear_values("l1") >= 1

        body = json.loads(kv_store.handle_api("l1", "1", "PUT", "api", '{"value": 7}'))
        assert body["key"] == "api"
        body = json.loads(kv_store.handle_api("l1", "1", "GET", "api", ""))
        assert body["value"] == 7
        body = json.loads(kv_store.handle_api("l1", "1", "GET", "", ""))
        assert any(item["key"] == "api" for item in body["items"])
        body = json.loads(kv_store.handle_api("l1", "2", "GET", "api", ""))
        assert "error" in body
        body = json.loads(kv_store.handle_api("l1", "1", "PUT", "bad key", '{"value": 1}'))
        assert "error" in body
    finally:
        dbpool.close_all()


def test_cron_expressions():
    import datetime as dt
    from zoneinfo import ZoneInfo
    from vlambda.cron import Cron, CronError

    tz = ZoneInfo("UTC")
    base = dt.datetime(2026, 1, 1, 0, 0, tzinfo=tz)
    assert Cron("*/15 * * * *").next_after(base, tz) == base + dt.timedelta(minutes=15)
    assert Cron("0 9 * * mon-fri").next_after(base, tz) == dt.datetime(2026, 1, 1, 9, 0, tzinfo=tz)
    assert Cron("0 0 1 * *").next_after(base, tz) == dt.datetime(2026, 2, 1, 0, 0, tzinfo=tz)
    assert Cron("0 0 * * sun").next_after(base, tz) == dt.datetime(2026, 1, 4, 0, 0, tzinfo=tz)
    assert Cron("0 0 * * 7").next_after(base, tz) == dt.datetime(2026, 1, 4, 0, 0, tzinfo=tz)
    assert Cron("0 0 * * 1").next_after(base, tz) == dt.datetime(2026, 1, 5, 0, 0, tzinfo=tz)
    with pytest.raises(CronError):
        Cron("* * *")
    with pytest.raises(CronError):
        Cron("60 * * * *")


def test_openapi_build(tmp_path, monkeypatch):
    import json
    import sqlite3
    from vlambda import dbpool, openapi

    db = tmp_path / "openapi.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE lambdas (slug TEXT, name TEXT, method TEXT, kind TEXT,"
        " owner_id INTEGER, summary TEXT, description TEXT, tags TEXT)"
    )
    conn.execute("INSERT INTO lambdas VALUES ('l1','demo','GET','code',1,'Demo','Desc','api,demo')")
    conn.execute("INSERT INTO lambdas VALUES ('p1','site','GET','project',1,'','','')")
    conn.execute("INSERT INTO lambdas VALUES ('l2','other','POST','code',2,'Other','','')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("VLAMBDA_DB", str(db))
    dbpool.close_all()
    dbpool.init(str(db))
    try:
        spec = openapi.build(1, "https://example.test")
        assert spec["openapi"].startswith("3.")
        assert spec["servers"][0]["url"] == "https://example.test"
        assert spec["paths"]["/f/l1"]["get"]["summary"] == "Demo"
        assert spec["paths"]["/f/l1"]["get"]["tags"] == ["api", "demo"]
        assert "/f/l2" not in spec["paths"]
        assert "/f/p1/{path}" in spec["paths"]
        body = json.loads(openapi.get_meta("l1", "1"))
        assert body["summary"] == "Demo"
        body = json.loads(openapi.set_meta("l1", "1", json.dumps({"summary": "New", "tags": "a,b"})))
        assert body["summary"] == "New" and body["tags"] == "a,b"
        body = json.loads(openapi.get_meta("l1", "2"))
        assert "error" in body
        body = json.loads(openapi.handle_api("1", "https://x"))
        assert "/f/l1" in body["paths"]
    finally:
        dbpool.close_all()


def test_schedules_store(tmp_path, monkeypatch):
    import json
    import sqlite3
    from vlambda import dbpool, schedules

    db = tmp_path / "schedules.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE lambdas (slug TEXT, owner_id INTEGER)")
    conn.execute("INSERT INTO lambdas VALUES ('l1', 1)")
    conn.execute("INSERT INTO lambdas VALUES ('l2', 2)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("VLAMBDA_DB", str(db))
    dbpool.close_all()
    dbpool.init(str(db))
    try:
        schedules.ensure_schema()
        item = schedules.create_schedule("l1", 1, {
            "name": "nightly", "cron": "0 3 * * *", "timezone": "UTC",
            "method": "POST", "path": "/job", "payload": '{"x": 1}',
        })
        assert item["name"] == "nightly" and item["next_run"] > 0
        assert schedules.list_schedules("l1", 1)[0]["name"] == "nightly"
        updated = schedules.update_schedule("l1", 1, item["id"], {"enabled": False})
        assert updated["enabled"] is False and updated["next_run"] is None
        body = json.loads(schedules.handle_api("l1", "1", "", "GET", "", ""))
        assert body["schedules"][0]["name"] == "nightly"
        body = json.loads(schedules.handle_api("l1", "2", "", "GET", "", ""))
        assert "error" in body
        body = json.loads(schedules.handle_api("l1", "1", "", "POST", "", '{"name":"bad"}'))
        assert "error" in body
        assert schedules.delete_schedule("l1", 1, item["id"]) is True
        assert schedules.list_schedules("l1", 1) == []
    finally:
        dbpool.close_all()


def test_egress_store_allowlist(tmp_path, monkeypatch):
    import json
    import sqlite3
    from vlambda import dbpool, egress_store

    db = tmp_path / "egress.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE lambdas (slug TEXT, owner_id INTEGER)")
    conn.execute(
        "CREATE TABLE lambda_egress (slug TEXT NOT NULL, pattern TEXT NOT NULL,"
        " PRIMARY KEY (slug, pattern))"
    )
    conn.execute("INSERT INTO lambdas VALUES ('l1', 1)")
    conn.execute("INSERT INTO lambdas VALUES ('l2', 2)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("VLAMBDA_DB", str(db))
    dbpool.close_all()
    dbpool.init(str(db))
    try:
        assert egress_store.set_hosts("l1", ["API.Example.com", "*.example.org"]) == [
            "api.example.com", "*.example.org"
        ]
        assert egress_store.list_hosts("l1") == ["*.example.org", "api.example.com"]
        assert egress_store.host_allowed("l1", "api.example.com")
        assert egress_store.host_allowed("l1", "sub.example.org")
        assert egress_store.host_allowed("l1", "example.org")
        assert not egress_store.host_allowed("l1", "other.example.com")
        with pytest.raises(egress_store.EgressError):
            egress_store.set_hosts("l1", ["https://example.com/path"])
        with pytest.raises(egress_store.EgressError):
            egress_store.set_hosts("l1", [f"h{i}.example.com" for i in range(21)])
        body = json.loads(egress_store.handle_api("l1", "1", "PUT", json.dumps({"hosts": ["x.test"]})))
        assert body["hosts"] == ["x.test"]
        body = json.loads(egress_store.handle_api("l1", "1", "GET", ""))
        assert body["hosts"] == ["x.test"]
        body = json.loads(egress_store.handle_api("l1", "2", "GET", ""))
        assert "error" in body
    finally:
        dbpool.close_all()


def test_capability_broker_egress(tmp_path, monkeypatch):
    import http.server
    import json
    import sqlite3
    import threading
    from vlambda import broker, dbpool
    from vlambda.capabilities import CapabilityClient, CapabilityError

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"ok": True, "path": self.path}).encode()
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

    db = tmp_path / "egress-broker.db"
    sock = tmp_path / "egress.sock"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE lambdas (slug TEXT, owner_id INTEGER)")
    conn.execute("CREATE TABLE lambda_kv (slug TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, expires_at REAL, updated_at REAL NOT NULL, PRIMARY KEY (slug, key))")
    conn.execute("CREATE TABLE lambda_egress (slug TEXT NOT NULL, pattern TEXT NOT NULL, PRIMARY KEY (slug, pattern))")
    conn.execute("INSERT INTO lambdas VALUES ('l1', 1)")
    conn.execute("INSERT INTO lambda_egress VALUES ('l1', 'localhost')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("VLAMBDA_DB", str(db))
    monkeypatch.setenv("VLAMBDA_BROKER_SOCKET", str(sock))
    monkeypatch.setenv("VLAMBDA_EGRESS_ALLOW_HTTP", "1")
    dbpool.close_all()
    dbpool.init(str(db))
    broker.stop()
    try:
        assert broker.start()
        client = CapabilityClient(str(sock), broker.issue("l1", "req-egress"))
        with pytest.raises(CapabilityError, match="not public"):
            client.fetch(f"http://localhost:{port}/private")
        monkeypatch.setenv("VLAMBDA_EGRESS_ALLOW_PRIVATE", "1")
        response = client.fetch(f"http://localhost:{port}/hello")
        assert response.status == 200
        assert response.json()["path"] == "/hello"
        with pytest.raises(CapabilityError, match="host not allowed"):
            client.fetch(f"http://127.0.0.1:{port}/nope")
        monkeypatch.delenv("VLAMBDA_EGRESS_ALLOW_HTTP", raising=False)
        with pytest.raises(CapabilityError, match="http egress is disabled"):
            client.fetch(f"http://localhost:{port}/hello")
    finally:
        broker.stop()
        upstream.shutdown()
        dbpool.close_all()


def test_lifecycle_drain():
    import threading
    from vlambda import lifecycle

    lifecycle.reset()
    assert lifecycle.begin() is True
    lifecycle.stop_accepting()
    assert lifecycle.begin() is False
    thread = threading.Thread(target=lambda: (time.sleep(0.1), lifecycle.end()))
    thread.start()
    assert lifecycle.drain(1.0) is True
    thread.join()
    lifecycle.reset()


def test_capability_context_is_thread_local():
    import threading
    from vlambda.capabilities import clear_current, current, set_current

    first = object()
    second = object()
    seen = {}
    set_current(first)
    try:
        def worker():
            set_current(second)
            seen["worker"] = current()
            clear_current()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        assert seen["worker"] is second
        assert current() is first
    finally:
        clear_current()


def test_capability_broker_kv(tmp_path, monkeypatch):
    import sqlite3
    from vlambda import broker, dbpool
    from vlambda.capabilities import CapabilityClient, CapabilityError

    db = tmp_path / "broker.db"
    sock = tmp_path / "broker.sock"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE lambdas (slug TEXT, owner_id INTEGER)")
    conn.execute(
        "CREATE TABLE lambda_kv (slug TEXT NOT NULL, key TEXT NOT NULL,"
        " value TEXT NOT NULL, expires_at REAL, updated_at REAL NOT NULL,"
        " PRIMARY KEY (slug, key))"
    )
    conn.execute("INSERT INTO lambdas VALUES ('l1', 1)")
    conn.execute("INSERT INTO lambdas VALUES ('l2', 2)")
    conn.commit()
    conn.close()

    monkeypatch.setenv("VLAMBDA_DB", str(db))
    monkeypatch.setenv("VLAMBDA_BROKER_SOCKET", str(sock))
    dbpool.close_all()
    dbpool.init(str(db))
    broker.stop()
    try:
        assert broker.start(), "broker did not start"
        token = broker.issue("l1", "req-1")
        client = CapabilityClient(str(sock), token)
        assert client.set("a", "hello") == "hello"
        assert client.get("a") == "hello"
        assert client.get("missing", "default") == "default"
        assert client.set("n", 1)
        assert client.incr("n") == 2
        assert client.set("bin", b"\x00\xff")
        assert client.get("bin") == b"\x00\xff"
        assert client.list()["a"] == "hello"
        assert client.delete("a") is True
        other = broker.issue("l2", "req-2")
        assert CapabilityClient(str(sock), other).get("n", "none") == "none"
        with pytest.raises(CapabilityError):
            CapabilityClient(str(sock), "bad-token").get("n")
        broker.release(token)
        broker.release(other)
    finally:
        broker.stop()
        dbpool.close_all()


def test_sandbox_allows_unix_blocks_inet(tmp_path):
    import os
    import subprocess
    import sys
    from vlambda.sandbox_os import sandbox_supported

    if not sandbox_supported():
        pytest.skip("Landlock is not available")
    script = (
        "from vlambda.pool import _landlock_read_paths\n"
        "from vlambda.sandbox_os import apply_sandbox\n"
        "import socket, sys\n"
        "apply_sandbox(_landlock_read_paths(), [sys.argv[1]], 5)\n"
        "try:\n"
        "    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()\n"
        "    print('unix-ok')\n"
        "except PermissionError:\n"
        "    print('unix-blocked')\n"
        "try:\n"
        "    socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()\n"
        "    print('inet-ok')\n"
        "except PermissionError:\n"
        "    print('inet-blocked')\n"
    )
    sandbox_dir = tmp_path / "rw"
    sandbox_dir.mkdir()
    env = os.environ.copy()
    env["PYTHONPATH"] = os.getcwd()
    proc = subprocess.run(
        [sys.executable, "-c", script, str(sandbox_dir)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "unix-ok" in proc.stdout
    assert "inet-blocked" in proc.stdout


def test_apply_env_replaces_previous_values(monkeypatch):
    import os
    from vlambda.runner import _apply_env

    monkeypatch.delenv("VL_TEST_VALUE", raising=False)
    monkeypatch.delenv("VLAMBDA_DB", raising=False)
    try:
        _apply_env({"VL_TEST_VALUE": "1", "OTHER": "a"})
        assert os.environ["VL_TEST_VALUE"] == "1"
        assert os.environ["OTHER"] == "a"

        _apply_env({"OTHER": "b"})
        assert "VL_TEST_VALUE" not in os.environ
        assert os.environ["OTHER"] == "b"

        _apply_env({"VLAMBDA_DB": "/evil.db", "OTHER": "c"})
        assert "VLAMBDA_DB" not in os.environ
        assert os.environ["OTHER"] == "c"
    finally:
        _apply_env({})


def test_cli_env_only_sends_given_category(monkeypatch):
    import json
    from argparse import Namespace
    import vlambda.cli as cli

    current = [
        {"key": "A", "secret": False, "value": "old"},
        {"key": "S", "secret": True, "value": "********"},
    ]
    payloads = []

    def fake_request(url, token, method="GET", body=None, headers=None):
        if method == "GET":
            return 200, {}, json.dumps({"env": current}).encode()
        payloads.append(json.loads(body))
        return 200, {}, b'{"env": []}'

    monkeypatch.setattr(cli, "_request", fake_request)

    cli.cmd_env(Namespace(url="http://x", token="t", slug="s", set=["B=1"], secret=None))
    assert payloads[-1] == {"vars": {"A": "old", "B": "1"}}

    cli.cmd_env(Namespace(url="http://x", token="t", slug="s", set=None, secret=["T=2"]))
    assert payloads[-1] == {"secrets": {"S": "********", "T": "2"}}

    cli.cmd_env(Namespace(url="http://x", token="t", slug="s", set=["B=1"], secret=["T=2"]))
    assert payloads[-1] == {"vars": {"A": "old", "B": "1"}, "secrets": {"S": "********", "T": "2"}}


def test_dbpool_separates_paths(tmp_path):
    import sqlite3
    from vlambda import dbpool

    dbpool.close_all()
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    for path in (first, second):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE items (value TEXT)")
        conn.commit()
        conn.close()
    try:
        with dbpool.get(str(first)) as conn:
            conn.execute("INSERT INTO items VALUES ('first')")
            conn.commit()
        with dbpool.get(str(second)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    finally:
        dbpool.close_all()


def test_auth_token_roundtrip():
    from vlambda.auth import create_token, verify_token
    token = create_token(42, "alice", "epoch-1")
    data = verify_token(token)
    assert data is not None
    assert data["sub"] == "42"
    assert data["username"] == "alice"
    assert data["epoch"] == "epoch-1"


def test_password_hash_roundtrip():
    import hashlib
    from vlambda.auth import check_password, hash_password

    stored = hash_password("secret")
    assert stored.startswith("pbkdf2:")
    assert check_password("secret", stored)
    assert not check_password("wrong", stored)
    salt = "legacy-salt"
    legacy = salt + ":" + hashlib.sha256((salt + "secret").encode()).hexdigest()
    assert check_password("secret", legacy)
    assert not check_password("wrong", legacy)
    assert not check_password("secret", "broken")


def test_auth_bad_token():
    from vlambda.auth import verify_token
    assert verify_token("not-a-token") is None
    assert verify_token("a.b.c") is None


def test_tasks_create_and_get():
    from vlambda.tasks import create_task, get_task
    pool = None
    # create_task expects pool with execute; use a dummy
    class DummyPool:
        def execute(self, *a, **k):
            return {"ok": True}
    tid = create_task("code", "GET", "/", "", b"", DummyPool())
    task = get_task(tid)
    assert task is not None
    assert task["status"] in ("pending", "completed")
    time.sleep(0.2)
    task = get_task(tid)
    assert task["status"] == "completed"


def test_task_ownership_filtering(monkeypatch):
    monkeypatch.setenv("VLAMBDA_TASK_RETRIES", "0")
    monkeypatch.setenv("VLAMBDA_TASK_RETRY_DELAY", "0.01")
    from vlambda import tasks

    class FailingPool:
        def execute(self, *a, **k):
            raise RuntimeError("boom")

    tid = tasks.create_task("code", "GET", "/", "", b"", FailingPool(),
                            owner_id=41, slug="l1")
    deadline = time.time() + 3
    while time.time() < deadline:
        task = tasks.get_task(tid, 41)
        if task is not None and task["status"] == "failed":
            break
        time.sleep(0.05)
    assert tasks.get_task(tid, 41)["status"] == "failed"
    assert tasks.get_task(tid, 42) is None
    assert tasks.get_task(tid, 0) is not None
    assert any(item["task_id"] == tid for item in tasks.get_dlq(41))
    assert not any(item["task_id"] == tid for item in tasks.get_dlq(42))


def test_project_safe_extract_zip_slip():
    import io, zipfile
    from vlambda.project import _safe_extract
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.py", "x=1")
    buf.seek(0)
    zf = zipfile.ZipFile(buf)
    with pytest.raises(ValueError):
        _safe_extract(zf, tempfile.mkdtemp())


def test_project_archive_size_limit(tmp_path, monkeypatch):
    import io
    import zipfile
    from vlambda.project import _safe_extract
    monkeypatch.setenv("VLAMBDA_PROJECT_MAX_MB", "1")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("big.bin", b"0" * (2 * 1024 * 1024))
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        with pytest.raises(ValueError):
            _safe_extract(zf, str(tmp_path / "out"))


def test_dlq_records_failed_task(monkeypatch):
    import os
    os.environ["VLAMBDA_TASK_RETRIES"] = "1"
    os.environ["VLAMBDA_TASK_RETRY_DELAY"] = "0.01"
    from vlambda import tasks
    before = len(tasks.get_dlq())

    class FailingPool:
        def execute(self, *a, **k):
            raise RuntimeError("boom")

    tid = tasks.create_task("code", "GET", "/", "", b"", FailingPool())
    time.sleep(0.5)
    task = tasks.get_task(tid)
    assert task is not None
    assert task["status"] == "failed"
    dlq = tasks.get_dlq()
    assert len(dlq) >= before + 1


def test_offline_reset(tmp_path):
    import sqlite3
    from vlambda.reset import offline_reset
    db = tmp_path / "vlambda.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE lambdas (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE lambda_versions (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE lambda_aliases (alias TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE lambda_env (slug TEXT, key TEXT)")
    conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE dlq (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO users (id) VALUES (1)")
    conn.execute("INSERT INTO lambdas (id) VALUES (1)")
    conn.execute("INSERT INTO lambda_versions (id) VALUES (1)")
    conn.execute("INSERT INTO lambda_env VALUES ('l1', 'A')")
    conn.execute("INSERT INTO tasks (task_id) VALUES ('t1')")
    conn.execute("INSERT INTO dlq (id) VALUES (1)")
    conn.commit()
    conn.close()
    projects = tmp_path / "projects" / "p1"
    projects.mkdir(parents=True)
    (projects / "main.py").write_text("x = 1")
    packages = tmp_path / "packages" / "dep"
    packages.mkdir(parents=True)
    metrics = tmp_path / "metrics.db"
    m = sqlite3.connect(metrics)
    m.execute("CREATE TABLE metrics (id INTEGER PRIMARY KEY)")
    m.execute("CREATE TABLE metrics_daily (id INTEGER PRIMARY KEY)")
    m.execute("INSERT INTO metrics (id) VALUES (1)")
    m.commit()
    m.close()
    result = offline_reset(str(db), str(metrics), str(tmp_path / "projects"), str(tmp_path / "packages"))
    assert result["rows"]["users"] == 1
    assert result["rows"]["lambda_env"] == 1
    assert result["rows"]["tasks"] == 1
    assert result["rows"]["dlq"] == 1
    assert not (tmp_path / "projects" / "p1").exists()
    assert not (tmp_path / "packages" / "dep").exists()
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM lambdas").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM lambda_env").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dlq").fetchone()[0] == 0
    conn.close()


def test_package_spec_validation():
    from vlambda.packages import _validate
    assert _validate("six") == "six"
    assert _validate("requests==2.32.0") == "requests==2.32.0"
    assert _validate("uvicorn[standard]>=0.30") == "uvicorn[standard]>=0.30"
    with pytest.raises(ValueError):
        _validate("git+https://example.com/x.git")
    with pytest.raises(ValueError):
        _validate("../evil")
    with pytest.raises(ValueError):
        _validate("-e .")


def test_metrics_aggregate(tmp_path):
    from vlambda.metrics import init, record, close, aggregate
    db = str(tmp_path / "metrics.db")
    init(db, 1)
    record("r1", "slug1", "GET", "/f/x", 1.0, "")
    record("r2", "slug1", "GET", "/f/x", 3.0, "")
    close()
    init(db, 1)
    data = aggregate(1)
    assert data["requests_total"] >= 2
    item = next(i for i in data["per_lambda"] if i["slug"] == "slug1")
    assert item["invocations"] >= 2
    assert abs(item["latency_avg_ms"] - 2.0) < 0.01
    close()


def test_logs_persistence(tmp_path):
    from vlambda.metrics import init, record_log, close, query_logs
    db = str(tmp_path / "metrics.db")
    init(db, 1)
    record_log("req1", "slug1", "info", "hello log")
    close()
    init(db, 1)
    data = query_logs(1)
    assert data["count"] >= 1
    assert any(i["message"] == "hello log" and i["slug"] == "slug1" for i in data["items"])
    close()


def test_delete_metrics_and_logs(tmp_path):
    from vlambda.metrics import (
        init, record, record_log, close, aggregate, query_logs,
        delete_metrics, delete_logs,
    )
    db = str(tmp_path / "metrics.db")
    init(db, 7)
    record("r1", "slug1", "GET", "/f/x", 1.0, "")
    record("r2", "slug1", "GET", "/f/x", 2.0, "")
    record_log("r1", "slug1", "info", "hello log")
    close()
    init(db, 7)

    # Recent data survives a prune of older entries.
    assert delete_metrics(1, "slug1")["deleted"] == 0
    assert aggregate(1)["requests_total"] == 2

    assert delete_metrics(0, "slug1")["deleted"] >= 2
    assert aggregate(1)["requests_total"] == 0
    assert delete_logs(0, "slug1")["deleted"] >= 1
    assert query_logs(7, "slug1")["count"] == 0
    close()


def test_new_slug_unique():
    from vlambda.transfer import _new_slug
    assert len({_new_slug("p", "same-content") for _ in range(50)}) == 50


def test_transfer_normalize_files():
    from vlambda.transfer import _normalize_files
    files, err = _normalize_files({"main.py": "x", "../evil": "y"})
    assert err and not files
    files, err = _normalize_files({"main.py": "x"})
    assert not err and files["main.py"] == "x"


def test_transfer_read_project_files(tmp_path):
    from vlambda.transfer import _read_project_files
    (tmp_path / "main.py").write_text("print('hi')")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "lib.py").write_text("should be skipped")
    (tmp_path / "logo.bin").write_bytes(b"\xff\xfe\x00")
    files, skipped = _read_project_files(str(tmp_path))
    assert files["main.py"] == "print('hi')"
    assert "vendor/lib.py" not in files
    assert "logo.bin" in skipped


def test_transfer_export_code(tmp_path, monkeypatch):
    import json
    import sqlite3
    db = tmp_path / "transfer.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE lambdas (slug TEXT, name TEXT, code TEXT, method TEXT, kind TEXT,"
        " entrypoint TEXT, project_dir TEXT, timeout INTEGER, memory_mb INTEGER,"
        " version INTEGER, owner_id INTEGER)"
    )
    conn.execute(
        "INSERT INTO lambdas VALUES ('l1', 'demo', 'def handler(request): return 1',"
        " 'GET', 'code', '', '', 0, 0, 1, 7)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("VLAMBDA_DB", str(db))
    from vlambda.transfer import export_lambda
    data = json.loads(export_lambda("l1", "7"))
    assert data["code"].startswith("def handler")
    assert data["format"] == "vlambda/v1"
    assert export_lambda("l1", "8") == ""


def test_async_handler_resolution():
    import asyncio

    from vlambda.async_support import resolve

    async def handler(value):
        await asyncio.sleep(0)
        return {"value": value}

    async def failing():
        raise ValueError("boom")

    assert resolve(handler(3)) == {"value": 3}
    assert resolve({"ok": True}) == {"ok": True}
    with pytest.raises(ValueError, match="boom"):
        resolve(failing())


def test_code_disk_cache_is_scoped_and_atomic(tmp_path, monkeypatch):
    from vlambda import cache as code_cache

    monkeypatch.setattr(code_cache, "_CACHE_DIR", str(tmp_path))
    code = "def handler(request): return 1"
    path = code_cache._disk_path(code)
    assert code_cache._CACHE_TAG in path

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"not a code object")
    assert code_cache._load_from_disk(code) is None

    code_cache.invalidate(code)
    code_cache._save_to_disk(code, compile(code, "<lambda>", "exec"))
    loaded = code_cache._load_from_disk(code)
    assert loaded is not None and loaded.co_filename == "<lambda>"


def test_async_project_handler(tmp_path):
    from vlambda.project import execute_project

    (tmp_path / "main.py").write_text(
        "async def value():\n"
        "    return 21\n\n"
        "async def handler(request):\n"
        "    return {'value': await value()}\n",
        encoding="utf-8",
    )
    assert execute_project(str(tmp_path), "main.py", None) == {"value": 21}
