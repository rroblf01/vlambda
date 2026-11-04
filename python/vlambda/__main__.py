"""CLI entry point: python -m vlambda."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "reset":
        from vlambda.reset import main as reset_main
        raise SystemExit(reset_main(sys.argv[2:]))
    if any(arg in ("invoke", "deploy", "logs", "env", "task", "kv", "egress", "schedule", "openapi") for arg in sys.argv[1:]):
        from vlambda.cli import main as cli_main
        raise SystemExit(cli_main(sys.argv[1:]))

    parser = argparse.ArgumentParser(prog="vlambda", description="vlambda server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("VLAMBDA_PORT", "8080")))
    parser.add_argument("--sqlite_path", default=os.environ.get("VLAMBDA_DB", "./vlambda.db"))
    parser.add_argument("--num_coroutines", type=int, default=int(os.environ.get("VLAMBDA_COROUTINES", "10")))
    parser.add_argument("--max_body_size", type=int, default=int(os.environ.get("VLAMBDA_MAX_BODY", "1048576")))
    parser.add_argument("--rate_limit", type=int, default=int(os.environ.get("VLAMBDA_RATE_LIMIT", "100")))
    parser.add_argument("--invoke_timeout", type=int, default=int(os.environ.get("VLAMBDA_TIMEOUT", "30")))
    parser.add_argument("--max_memory_mb", type=int, default=int(os.environ.get("VLAMBDA_MAX_MEMORY_MB", "0")))
    parser.add_argument("--cors_origin", default=os.environ.get("VLAMBDA_CORS", "*"))
    parser.add_argument("--cert", default=os.environ.get("VLAMBDA_CERT", ""))
    parser.add_argument("--key", default=os.environ.get("VLAMBDA_KEY", ""))
    args = parser.parse_args()
    os.environ["VLAMBDA_DB"] = args.sqlite_path
    if not os.environ.get("VLAMBDA_SECRET"):
        print("WARNING: VLAMBDA_SECRET is not set; using an insecure default for auth tokens.",
              file=sys.stderr)
    if sys.version_info < (3, 15) and os.environ.get("VLAMBDA_SANDBOX", "").lower() not in ("fork", "forklandlock", "landlock", "nsjail", "process") \
            and os.environ.get("VLAMBDA_ISOLATE", "0") != "1":
        print("WARNING: no sandbox selected; lambda code runs in the server process.",
              file=sys.stderr)

    # Try to use jemalloc for better allocation performance
    try:
        import ctypes
        ctypes.CDLL("libjemalloc.so.2", mode=ctypes.RTLD_GLOBAL)
    except OSError:
        pass

    from vlambda.sandbox import execute_lambda

    def _validate_code(code):
        import ast
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return f"syntax error: {e}"
        # Check for handler function
        has_handler = any(
            isinstance(node, ast.FunctionDef) and node.name == "handler"
            for node in ast.walk(tree)
        )
        if not has_handler:
            return "missing handler function"
        return ""  # valid
    from vlambda.auth import verify_token, create_token, hash_password, check_password, new_auth_epoch
    import sqlite3
    from vlambda.request import Request
    from vlambda_native import start_server, db_init, register_pool, register_auth_verifier, register_auth_register, register_auth_login, configure, register_validate_code, register_ui_html, register_get_asset, register_get_project_json, register_save_project_json, register_list_versions, register_list_aliases, register_admin_reset, register_admin_create_user, register_list_packages, register_install_packages, register_create_task, register_get_task, register_record_metric, register_query_metrics, register_query_stats, register_query_summary, register_query_aggregate, register_query_logs, register_delete_metrics, register_delete_logs, register_export_metrics, register_pool_stats, register_run_project, register_create_project, register_delete_project, register_schedule_delete, register_get_dlq, register_export_lambda, register_import_lambda, register_duplicate_lambda, register_list_env, register_save_env, register_kv, register_egress, register_schedules, register_meta, register_openapi, register_shutdown, set_overloaded

    if not db_init(args.sqlite_path):
        print("Failed to initialize database", file=sys.stderr)
        sys.exit(1)

    from vlambda.dbpool import init as pool_init
    pool_init(args.sqlite_path)

    from vlambda import broker as _broker, lifecycle as _lifecycle
    if _broker.start():
        os.environ["VLAMBDA_BROKER_SOCKET"] = _broker.SOCKET_PATH

    # GC tuning: disable during request handling, collect periodically
    import gc
    gc.set_threshold(0, 0, 0)  # disable automatic GC during requests

    metrics_enabled = os.environ.get("VLAMBDA_METRICS_ENABLED", "1") == "1"
    metrics_sample = int(os.environ.get("VLAMBDA_METRICS_SAMPLE", "1"))
    registration_enabled = os.environ.get("VLAMBDA_REGISTRATION", "1") == "1"

    configure(
        args.max_body_size, args.rate_limit, args.invoke_timeout,
        args.max_memory_mb, args.cors_origin, args.cert, args.key,
        metrics_enabled, metrics_sample, registration_enabled,
    )

    metrics_db = os.environ.get("VLAMBDA_METRICS_DB", args.sqlite_path.replace(".db", "_metrics.db"))
    metrics_retention = int(os.environ.get("VLAMBDA_METRICS_RETENTION", "7"))

    from vlambda.metrics import init as metrics_init
    metrics_init(metrics_db, metrics_retention)

    from vlambda.pool import Pool
    pool = Pool(args.num_coroutines)

    from vlambda.tasks import create_task, get_task, start as start_tasks
    start_tasks(pool)

    def _task_owner(owner_id):
        try:
            return int(owner_id)
        except (TypeError, ValueError):
            return 0

    def _create_task(code, method, path, query, body, owner_id="0", slug="", request_meta=""):
        import json
        task_id = create_task(code, method, path, query, body, pool,
                              owner_id=_task_owner(owner_id), slug=slug,
                              request_meta=request_meta)
        return json.dumps({"task_id": task_id, "status": "pending"})

    def _get_task(task_id, owner_id="0"):
        import json
        task = get_task(task_id, _task_owner(owner_id))
        if task is None:
            return json.dumps({"error": "task not found"})
        result = {"task_id": task_id, "status": task["status"], "attempts": task.get("attempts", 0)}
        if task["status"] == "completed":
            r = task["result"]
            result["result"] = json.loads(r) if isinstance(r, str) else r
        elif task["status"] == "failed":
            result["error"] = task["error"]
        return json.dumps(result)

    # A GET response is only cached when the handler does not read per-request
    # context: headers, cookies, body, env or remote address would otherwise be
    # shared with other callers, and logs would silently disappear.
    _CACHE_BYPASS = (
        ".header(", ".headers", ".cookies", ".env", ".body", ".json(", ".form(",
        ".remote_ip", ".host", ".scheme", ".request_id", ".slug", ".log(",
        "random", "time.time", "datetime.now", "datetime.utcnow",
    )
    _RESPONSE_CACHE_TTL = int(os.environ.get("VLAMBDA_RESP_CACHE_TTL", "0"))

    def _response_cacheable(code):
        return _RESPONSE_CACHE_TTL > 0 and not any(token in code for token in _CACHE_BYPASS)

    def _pool_execute(request_id, slug, code, method, path, query, body, timeout=0, memory_mb=0, request_meta=""):
        method = method or "GET"
        path = path or "/"
        query = query or ""
        body = body or b""
        import json as _json
        if not _lifecycle.accepting():
            return _json.dumps({
                "status": 503,
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "body": _json.dumps({"error": "shutting down"}),
                "body_b64": "",
            })
        # Response cache for idempotent GET requests, storing the final payload.
        cache_key = None
        if method == "GET" and _response_cacheable(code):
            cache_key = f"{slug}:{code}:{path}:{query}"
            from vlambda.cache import get_response
            cached = get_response(cache_key)
            if cached is not None:
                return cached

        def _json_default(value):
            if isinstance(value, (bytes, bytearray)):
                return bytes(value).decode("utf-8", "replace")
            if isinstance(value, set):
                return list(value)
            return str(value)

        try:
            import orjson
            _dumps = orjson.dumps
        except ImportError:
            _dumps = lambda x: _json.dumps(x, default=_json_default).encode()
        vendor_dir = ""
        if slug:
            try:
                from vlambda.packages import lambda_target
                import os as _os
                candidate = lambda_target(slug)
                if _os.path.isdir(candidate):
                    vendor_dir = candidate
            except Exception:
                pass
        if not timeout:
            timeout = args.invoke_timeout
        if not _lifecycle.begin():
            return _json.dumps({
                "status": 503,
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "body": _json.dumps({"error": "shutting down"}),
                "body_b64": "",
            })
        try:
            request_meta = _meta_with_env(slug, request_meta)
            request_meta, broker_token = _broker.attach(request_meta, slug, request_id)
            try:
                result = pool.execute(code, method, path, query, body, timeout, memory_mb, vendor_dir, request_id, slug, request_meta)
            finally:
                _broker.release(broker_token)
        finally:
            _lifecycle.end()
        from vlambda.response import serialize
        if isinstance(result, dict) and "status" in result and ("body" in result or "body_b64" in result):
            response = result
        elif isinstance(result, dict) and set(result.keys()) == {"error"}:
            response = {
                "status": 500,
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "body": _json.dumps(result),
                "body_b64": "",
            }
        else:
            response = serialize(result)
        payload = _json.dumps(response)
        try:
            status = int(response.get("status", 200))
        except (TypeError, ValueError):
            status = 500
        if cache_key is not None and status < 500:
            from vlambda.cache import set_response
            set_response(cache_key, payload)
        return payload

    register_pool(_pool_execute)

    def _auth_verify(token):
        data = verify_token(token)
        if data is None:
            return -1
        try:
            uid = int(data.get("sub", -1))
        except (ValueError, TypeError):
            return -1
        epoch = str(data.get("epoch", ""))
        if uid < 0 or not epoch:
            return -1
        from vlambda.dbpool import get as db_get
        try:
            with db_get(args.sqlite_path) as conn:
                row = conn.execute("SELECT auth_epoch FROM users WHERE id = ?", (uid,)).fetchone()
        except Exception:
            return -1
        if row is None or row[0] != epoch:
            return -1
        return uid

    register_auth_verifier(_auth_verify)

    # Auth: register user
    def _auth_register(username, password):
        from vlambda.dbpool import get as db_get
        epoch = new_auth_epoch()
        try:
            with db_get(args.sqlite_path) as conn:
                conn.execute("INSERT INTO users (username, password_hash, auth_epoch) VALUES (?, ?, ?)",
                             (username, hash_password(password), epoch))
                conn.commit()
                uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
                return uid
        except Exception as e:
            if "UNIQUE" in str(e) or "IntegrityError" in str(type(e).__name__):
                return -1
            return -1

    # Auth: login
    def _auth_login(username, password):
        from vlambda.dbpool import get as db_get
        with db_get(args.sqlite_path) as conn:
            row = conn.execute("SELECT id, password_hash, auth_epoch FROM users WHERE username = ?", (username,)).fetchone()
            if row is None:
                return ""
            uid, stored, epoch = row
            if not check_password(password, stored):
                return ""
            if not epoch:
                epoch = new_auth_epoch()
                conn.execute("UPDATE users SET auth_epoch = ? WHERE id = ?", (epoch, uid))
                conn.commit()
            return create_token(uid, username, epoch)

    # Register V bridges
    from vlambda_native import register_auth_register, register_auth_login
    register_auth_register(_auth_register)
    register_auth_login(_auth_login)

    register_validate_code(_validate_code)

    # Load UI HTML
    from vlambda.assets import UI_HTML as _ui_html

    def _get_ui_html():
        return _ui_html

    register_ui_html(_get_ui_html)

    from vlambda.assets import get_asset as _get_asset_impl

    def _get_asset(name):
        try:
            data = _get_asset_impl(name)
            return data if data is not None else b""
        except Exception as e:
            print("get_asset error", name, repr(e), flush=True)
            return ("ERR:" + str(e)).encode()

    register_get_asset(_get_asset)

    from vlambda.reset import _admin_token_ok, in_process_reset

    def _admin_reset(token):
        if not _admin_token_ok(token):
            return json.dumps({"error": "forbidden"})
        try:
            return json.dumps(in_process_reset())
        except Exception as e:
            return json.dumps({"error": str(e)})

    register_admin_reset(_admin_reset)

    def _admin_create_user(token, username, password):
        if not _admin_token_ok(token):
            return json.dumps({"error": "forbidden"})
        uid = _auth_register(username, password)
        if uid < 0:
            return json.dumps({"error": "user already exists"})
        return json.dumps({"user_id": uid})

    register_admin_create_user(_admin_create_user)

    from vlambda.packages import list_for as _list_packages, install_for as _install_packages

    def _list_packages_cb(slug, kind, project_dir):
        return _list_packages(slug, kind, project_dir)

    def _install_packages_cb(slug, kind, project_dir, body):
        return _install_packages(slug, kind, project_dir, body)

    register_list_packages(_list_packages_cb)
    register_install_packages(_install_packages_cb)

    from vlambda.project import (
        get_project_json as _get_project_json,
        save_project_json as _save_project_json,
        list_versions as _list_versions,
        list_aliases as _list_aliases,
    )

    def _get_project_json_cb(project_dir, entrypoint, slug, method):
        return _get_project_json(project_dir, entrypoint, slug, method)

    def _save_project_json_cb(project_dir, body, entrypoint):
        return _save_project_json(project_dir, body, entrypoint)

    def _list_versions_cb(slug):
        return _list_versions(slug)

    def _list_aliases_cb(slug):
        return _list_aliases(slug)

    register_get_project_json(_get_project_json_cb)
    register_save_project_json(_save_project_json_cb)
    register_list_versions(_list_versions_cb)
    register_list_aliases(_list_aliases_cb)

    from vlambda.assets import FAVICON as _favicon

    def _get_favicon():
        return _favicon

    from vlambda_native import register_favicon
    register_favicon(_get_favicon)

    register_create_task(_create_task)
    register_get_task(_get_task)

    import atexit
    from vlambda.env import get_env as _lambda_env, list_env as _env_list, save_env as _env_save
    from vlambda.metrics import record as _metrics_record, query as _metrics_query, stats as _metrics_query_stats, summary as _metrics_summary, aggregate as _metrics_aggregate, query_logs as _metrics_logs, export_csv as _metrics_export, delete_metrics as _metrics_delete, delete_logs as _metrics_delete_logs
    import json as _json

    def _meta_with_env(slug, request_meta):
        try:
            meta = _json.loads(request_meta or "{}")
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta["env"] = _lambda_env(slug) if slug else {}
        return _json.dumps(meta)

    def _list_env_cb(slug, owner_id):
        return _env_list(slug, owner_id)

    def _save_env_cb(slug, owner_id, body):
        return _env_save(slug, owner_id, body)

    register_list_env(_list_env_cb)
    register_save_env(_save_env_cb)

    from vlambda.kv_store import handle_api as _kv_api

    def _kv_cb(slug, owner_id, method, key, body, query=""):
        return _kv_api(slug, owner_id, method, key, body, query)

    register_kv(_kv_cb)

    from vlambda.egress_store import handle_api as _egress_api

    def _egress_cb(slug, owner_id, method, body):
        return _egress_api(slug, owner_id, method, body)

    register_egress(_egress_cb)

    from vlambda.schedules import handle_api as _schedules_api

    def _schedules_cb(slug, owner_id, action, method, schedule_id, body):
        return _schedules_api(slug, owner_id, action, method, schedule_id, body)

    register_schedules(_schedules_cb)

    from vlambda.openapi import get_meta as _get_meta, set_meta as _set_meta, handle_api as _openapi_api

    def _meta_cb(slug, owner_id, method, body):
        return _get_meta(slug, owner_id) if method == "GET" else _set_meta(slug, owner_id, body)

    register_meta(_meta_cb)

    def _openapi_cb(owner_id, base_url):
        return _openapi_api(owner_id, base_url)

    register_openapi(_openapi_cb)

    def _record_metric(request_id, slug, method, path, latency_ms, error):
        _metrics_record(request_id, slug, method, path, float(latency_ms), error)

    def _owner(value):
        try:
            return int(value)
        except (ValueError, TypeError):
            return 0

    def _query_metrics(days, owner_id="0"):
        try:
            d = int(days)
        except (ValueError, TypeError):
            d = 7
        return _json.dumps(_metrics_query(d, owner_id=_owner(owner_id)))

    register_record_metric(_record_metric)
    register_query_metrics(_query_metrics)

    def _query_stats(days, slug="", owner_id="0"):
        try:
            d = int(days)
        except (ValueError, TypeError):
            d = 7
        return _json.dumps(_metrics_query_stats(d, slug, owner_id=_owner(owner_id)))

    register_query_stats(_query_stats)


    def _query_summary(days, slug="", owner_id="0"):
        try:
            d = int(days)
        except (ValueError, TypeError):
            d = 7
        return _json.dumps(_metrics_summary(d, slug, owner_id=_owner(owner_id)))

    def _export_metrics(days, slug="", owner_id="0"):
        try:
            d = int(days)
        except (ValueError, TypeError):
            d = 7
        return _metrics_export(d, slug, owner_id=_owner(owner_id))

    def _pool_stats():
        import json
        stats = pool.stats()
        stats["instance_id"] = f"{os.environ.get('VLAMBDA_INSTANCE_ID') or os.uname().nodename}:{os.getpid()}"
        stats["inflight"] = _lifecycle.inflight()
        stats["accepting"] = _lifecycle.accepting()
        return json.dumps(stats)

    register_query_summary(_query_summary)

    def _query_aggregate(days, slug="", owner_id="0"):
        try:
            d = int(days)
        except (ValueError, TypeError):
            d = 7
        return _json.dumps(_metrics_aggregate(d, slug, owner_id=_owner(owner_id)))

    register_query_aggregate(_query_aggregate)

    def _query_logs(days, slug="", owner_id="0"):
        try:
            d = int(days)
        except (ValueError, TypeError):
            d = 1
        return _json.dumps(_metrics_logs(d, slug, owner_id=_owner(owner_id)))

    register_query_logs(_query_logs)

    def _delete_metrics(days, slug="", owner_id="0"):
        try:
            d = int(days)
        except (ValueError, TypeError):
            d = 0
        return _json.dumps(_metrics_delete(d, slug, owner_id=_owner(owner_id)))

    def _delete_logs(days, slug="", owner_id="0"):
        try:
            d = int(days)
        except (ValueError, TypeError):
            d = 0
        return _json.dumps(_metrics_delete_logs(d, slug, owner_id=_owner(owner_id)))

    register_delete_metrics(_delete_metrics)
    register_delete_logs(_delete_logs)
    register_export_metrics(_export_metrics)
    register_pool_stats(_pool_stats)

    from vlambda.project import create_project as _create_project, execute_project as _execute_project, delete_project as _delete_project_impl, schedule_delete as _schedule_delete_impl

    def _run_project(project_dir, entrypoint, method, path, query, body, request_id, slug, request_meta):
        import json
        timeout = args.invoke_timeout
        from vlambda.response import serialize
        if not _lifecycle.begin():
            return json.dumps({
                "status": 503,
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "body": json.dumps({"error": "shutting down"}),
                "body_b64": "",
            })
        try:
            try:
                request_meta = _meta_with_env(slug, request_meta)
                request_meta, broker_token = _broker.attach(request_meta, slug, request_id)
                try:
                    result = pool.execute_project(project_dir, entrypoint, method, path, query, body, timeout=timeout, request_id=request_id, slug=slug, request_meta=request_meta)
                finally:
                    _broker.release(broker_token)
            finally:
                _lifecycle.end()
            if isinstance(result, dict) and "status" in result and ("body" in result or "body_b64" in result):
                return json.dumps(result)
            if isinstance(result, dict) and set(result.keys()) == {"error"}:
                return json.dumps({
                    "status": 500,
                    "headers": [{"name": "Content-Type", "value": "application/json"}],
                    "body": json.dumps(result),
                    "body_b64": "",
                })
            return json.dumps(serialize(result))
        except Exception as e:
            return json.dumps({
                "status": 500,
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "body": json.dumps({"error": f"{type(e).__name__}: {e}"}),
                "body_b64": "",
            })

    def _scheduled_execute(slug, method, path, payload, request_id, schedule_name):
        import json
        import vlambda_native
        from vlambda.packages import lambda_target

        if not _lifecycle.begin():
            return {"status": 503, "body": json.dumps({"error": "shutting down"}),
                    "headers": [], "body_b64": ""}
        headers = {
            "X-Vlambda-Trigger": "schedule",
            "X-Vlambda-Schedule": schedule_name,
        }
        meta = json.dumps({"headers": headers, "remote_ip": "", "host": "", "scheme": "http"})
        path_only, _, query = path.partition("?")
        body = payload.encode("utf-8") if payload else b""
        broker_token = None
        try:
            meta = _meta_with_env(slug, meta)
            meta, broker_token = _broker.attach(meta, slug, request_id)
            project_info = vlambda_native.db_get_project(slug)
            if project_info:
                parts = project_info.split("|")
                project_dir = parts[0] if parts else ""
                entrypoint = parts[1] if len(parts) > 1 else "main.py"
                return pool.execute_project(
                    project_dir, entrypoint, method, path_only, query, body,
                    timeout=args.invoke_timeout, request_id=request_id,
                    slug=slug, request_meta=meta,
                )
            code_info = vlambda_native.db_get_lambda_code(slug)
            if not code_info:
                return {"status": 404, "body": json.dumps({"error": "lambda not found"}),
                        "headers": [], "body_b64": ""}
            parts = code_info.split("|")
            code = parts[0] if parts else ""
            timeout = int(parts[2]) if len(parts) > 2 and parts[2] else args.invoke_timeout
            memory_mb = int(parts[3]) if len(parts) > 3 and parts[3] else 0
            vendor_dir = ""
            try:
                candidate = lambda_target(slug)
                if os.path.isdir(candidate):
                    vendor_dir = candidate
            except Exception:
                pass
            return pool.execute(
                code, method, path_only, query, body, timeout, memory_mb,
                vendor_dir, request_id, slug, meta,
            )
        finally:
            _broker.release(broker_token)
            _lifecycle.end()

    def _delete_project(project_dir):
        _delete_project_impl(project_dir)
        return None

    register_run_project(_run_project)
    register_create_project(_create_project)
    register_delete_project(_delete_project)

    from vlambda.schedules import start as _schedules_start, stop as _schedules_stop
    _schedules_start(_scheduled_execute)

    def _drain_and_stop():
        _lifecycle.stop_accepting()
        _schedules_stop()
        _lifecycle.drain()
        import vlambda.tasks as _drain_tasks
        _drain_tasks.stop()
        return "{}"

    register_shutdown(_drain_and_stop)

    def _schedule_delete(project_dir):
        _schedule_delete_impl(project_dir)
        return None

    register_schedule_delete(_schedule_delete)

    from vlambda.transfer import export_lambda as _export_lambda, import_lambda as _import_lambda, duplicate_lambda as _duplicate_lambda
    register_export_lambda(_export_lambda)
    register_import_lambda(_import_lambda)
    register_duplicate_lambda(_duplicate_lambda)

    def _get_dlq(owner_id="0"):
        import json
        from vlambda.tasks import get_dlq
        return json.dumps(get_dlq(_task_owner(owner_id)))

    register_get_dlq(_get_dlq)

    # Periodically remove project directories no lambda references.
    try:
        from vlambda.project import gc_orphans as _gc_orphans
        def _project_gc():
            import time, threading
            from vlambda import schedules as _schedules
            while True:
                try:
                    if not _schedules.running() or _schedules.is_leader():
                        removed = _gc_orphans()
                        if removed:
                            print(f"project gc removed {removed} orphan directories", file=sys.stderr)
                except Exception as e:
                    print(f"project gc failed: {e}", file=sys.stderr)
                time.sleep(300)
        import threading as _threading
        _threading.Thread(target=_project_gc, daemon=True).start()
    except Exception as e:
        print(f"project gc not started: {e}", file=sys.stderr)

    import vlambda.tasks as _tasks
    atexit.register(_tasks.stop)
    atexit.register(pool.stop)

    # Run server in a thread so signals can be handled
    import threading
    _server_done = threading.Event()

    def _run_server():
        rc = start_server(args.port, args.sqlite_path, args.num_coroutines)
        _server_done.set()

    _server_thread = threading.Thread(target=_run_server, daemon=True)
    _server_thread.start()

    # Memory monitoring thread (if max_memory_mb > 0)
    if args.max_memory_mb > 0:
        import threading, time, resource
        # A hard RLIMIT_AS for the whole process starves V/Python threads and
        # mmap. Use the Docker cgroup or the per-invocation sandbox limits
        # instead. It can be enabled explicitly for constrained environments.
        if os.environ.get("VLAMBDA_HARD_MEMORY_LIMIT", "0") == "1":
            limit_bytes = args.max_memory_mb * 1024 * 1024
            try:
                resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
                print(f"Hard memory limit set to {args.max_memory_mb}MB", file=sys.stderr)
            except (ValueError, OSError) as e:
                print(f"Could not set hard memory limit: {e}", file=sys.stderr)
        def _mem_monitor():
            limit_bytes = args.max_memory_mb * 1024 * 1024
            overloaded = False
            while True:
                time.sleep(5)
                try:
                    import gc
                    gc.collect()
                    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
                    if usage > limit_bytes * 0.9 and not overloaded:
                        overloaded = True
                        set_overloaded(True)
                        print(f"WARNING: memory usage {usage/1024/1024:.1f}MB approaching limit {args.max_memory_mb}MB", file=sys.stderr)
                    elif usage < limit_bytes * 0.8 and overloaded:
                        overloaded = False
                        set_overloaded(False)
                except Exception:
                    pass
        t = threading.Thread(target=_mem_monitor, daemon=True)
        t.start()

    # Graceful shutdown
    def _shutdown(signum, frame):
        print("\nShutting down gracefully...", file=sys.stderr)
        _lifecycle.stop_accepting()
        _schedules_stop()
        _lifecycle.drain()
        _tasks.stop()
        pool.stop()
        _broker.stop()
        from vlambda.dbpool import close_all
        close_all()
        gc.collect()
        _server_done.set()
        os._exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(f"Starting vlambda on port {args.port} (db={args.sqlite_path}, coroutines={args.num_coroutines})")
    print(f"  max_body={args.max_body_size}, rate_limit={args.rate_limit}/s, timeout={args.invoke_timeout}s, max_mem={args.max_memory_mb}MB")
    print("Press Ctrl+C to stop.")
    # Note: Ctrl+C may not work due to V's blocking listen_and_serve().
    # Use 'kill <pid>' or the /shutdown endpoint to stop.
    import time
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
