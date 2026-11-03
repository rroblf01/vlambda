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
    set_response("a", "1")
    set_response("b", "2")
    set_response("c", "3")
    assert get_response("a") is None
    assert get_response("c") == "3"


def test_pool_basic():
    pool = Pool(2)
    code = "def handler(request): return request.path.upper()"
    from vlambda.request import Request
    res = pool.execute(code, "GET", "/abc", "", b"")
    assert res == "/ABC"
    pool.stop()


def test_runner_env_strips_secrets(monkeypatch):
    from vlambda.pool import _runner_env
    monkeypatch.setenv("VLAMBDA_SECRET", "super-secret")
    monkeypatch.setenv("VLAMBDA_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("VLAMBDA_DB", "/data/db/vlambda.db")
    env = _runner_env()
    assert not any(key.startswith("VLAMBDA_") for key in env)


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


def test_auth_token_roundtrip():
    from vlambda.auth import create_token, verify_token
    token = create_token(42, "alice")
    data = verify_token(token)
    assert data is not None
    assert data["sub"] == "42"
    assert data["username"] == "alice"


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
    conn.execute("INSERT INTO users (id) VALUES (1)")
    conn.execute("INSERT INTO lambdas (id) VALUES (1)")
    conn.execute("INSERT INTO lambda_versions (id) VALUES (1)")
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
    assert not (tmp_path / "projects" / "p1").exists()
    assert not (tmp_path / "packages" / "dep").exists()
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM lambdas").fetchone()[0] == 0
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
