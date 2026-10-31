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
    with pytest.raises(ImportError):
        blocked_import("os")
    with pytest.raises(ImportError):
        blocked_import("subprocess")


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
