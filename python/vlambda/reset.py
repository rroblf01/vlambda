"""Reset all vlambda data.

Can run offline against the SQLite files or call a running server's
``/admin/reset`` endpoint when an admin token is available.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.request


def _clear_dir(path: str) -> int:
    removed = 0
    if not os.path.isdir(path):
        return 0
    for name in os.listdir(path):
        full = os.path.join(path, name)
        try:
            if os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full, ignore_errors=True)
            else:
                os.remove(full)
            removed += 1
        except OSError:
            pass
    return removed


def _delete_rows(db_path: str, tables: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not os.path.exists(db_path):
        return counts
    try:
        conn = sqlite3.connect(db_path, timeout=15)
        conn.execute("PRAGMA busy_timeout=15000")
        for table in tables:
            try:
                cur = conn.execute(f"DELETE FROM {table}")
                counts[table] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            except sqlite3.OperationalError:
                pass
        try:
            conn.execute("DELETE FROM sqlite_sequence")
        except sqlite3.OperationalError:
            pass
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        print(f"reset: could not clear {db_path}: {exc}", file=sys.stderr)
    return counts


def offline_reset(
    db_path: str,
    metrics_db: str,
    projects_dir: str,
    packages_dir: str,
    cache_dir: str | None = None,
) -> dict:
    groups = _delete_rows(db_path, ["users", "lambdas", "lambda_versions", "lambda_aliases"])
    metrics = _delete_rows(metrics_db, ["metrics", "metrics_daily"])
    removed_projects = _clear_dir(projects_dir)
    removed_packages = _clear_dir(packages_dir)
    removed_cache = _clear_dir(cache_dir) if cache_dir else 0
    return {
        "db": db_path,
        "metrics_db": metrics_db,
        "rows": groups,
        "metrics_rows": metrics,
        "projects_removed": removed_projects,
        "packages_removed": removed_packages,
        "cache_removed": removed_cache,
        "timestamp": time.time(),
    }


def online_reset(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url.rstrip("/") + "/admin/reset",
        method="POST",
        headers={"X-Admin-Token": token},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _admin_token_ok(token: str) -> bool:
    expected = os.environ.get("VLAMBDA_ADMIN_TOKEN", "")
    if not expected:
        return False
    return hmac.compare_digest(token or "", expected)


def in_process_reset() -> dict:
    """Reset data from inside the running server."""
    db_path = os.environ.get("VLAMBDA_DB", "./vlambda.db")
    metrics_db = os.environ.get(
        "VLAMBDA_METRICS_DB", db_path.replace(".db", "_metrics.db")
    )
    projects_dir = os.environ.get("VLAMBDA_PROJECTS_DIR", "/tmp/opencode/vlambda_projects")
    packages_dir = os.environ.get("VLAMBDA_PACKAGES_DIR", "/tmp/opencode/vlambda_packages")
    cache_dir = os.environ.get("VLAMBDA_CACHE_DIR", "/tmp/opencode/vlambda_cache")

    # Stop the metrics writer and SQLite pool before deleting their rows.
    try:
        from vlambda import metrics

        metrics.close()
    except Exception:
        pass
    try:
        from vlambda import dbpool

        dbpool.close_all()
    except Exception:
        pass

    result = offline_reset(db_path, metrics_db, projects_dir, packages_dir, cache_dir)

    # Clear Python-level caches.
    try:
        from vlambda import cache

        cache.clear()
        cache.invalidate_responses()
    except Exception:
        pass
    try:
        from vlambda import project

        with project._lock:
            project._cache.clear()
    except Exception:
        pass
    try:
        from vlambda import tasks

        with tasks._lock:
            tasks._tasks.clear()
        with tasks._DLQ_LOCK:
            tasks._DLQ.clear()
    except Exception:
        pass

    # Clear V-level caches.
    try:
        import vlambda_native

        vlambda_native.clear_caches()
    except Exception:
        pass

    # Re-open the metrics writer and SQLite pool with a clean database.
    try:
        from vlambda import metrics

        metrics.init(metrics_db, int(os.environ.get("VLAMBDA_METRICS_RETENTION", "7")))
    except Exception:
        pass
    try:
        from vlambda import dbpool

        dbpool.init(db_path)
    except Exception:
        pass

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vlambda reset", description="Delete all vlambda data")
    parser.add_argument("--yes", action="store_true", help="skip confirmation")
    parser.add_argument("--url", default=os.environ.get("VLAMBDA_URL", ""))
    parser.add_argument("--token", default=os.environ.get("VLAMBDA_ADMIN_TOKEN", ""))
    parser.add_argument("--db", default=os.environ.get("VLAMBDA_DB", "./vlambda.db"))
    parser.add_argument("--metrics-db", default=os.environ.get("VLAMBDA_METRICS_DB", ""))
    parser.add_argument(
        "--projects-dir",
        default=os.environ.get("VLAMBDA_PROJECTS_DIR", "/tmp/opencode/vlambda_projects"),
    )
    parser.add_argument(
        "--packages-dir",
        default=os.environ.get("VLAMBDA_PACKAGES_DIR", "/tmp/opencode/vlambda_packages"),
    )
    parser.add_argument(
        "--cache-dir",
        default=os.environ.get("VLAMBDA_CACHE_DIR", "/tmp/opencode/vlambda_cache"),
    )
    args = parser.parse_args(argv)

    if not args.yes:
        print("This deletes ALL users, lambdas, projects, packages and metrics.")
        answer = input("Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            print("Aborted.")
            return 1

    if args.url and args.token:
        try:
            result = online_reset(args.url, args.token)
            print(json.dumps(result, indent=2))
            return 0
        except Exception as exc:
            print(f"Online reset failed ({exc}); falling back to offline", file=sys.stderr)

    metrics_db = args.metrics_db or args.db.replace(".db", "_metrics.db")
    result = offline_reset(
        args.db, metrics_db, args.projects_dir, args.packages_dir, args.cache_dir
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
