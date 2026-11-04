"""Offline backup and restore for vlambda data.

The archive contains a consistent SQLite copy of the main and metrics
databases, the project tree and, optionally, installed packages.  Restoring is
an offline operation: stop the server first so the database files are not in
use.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from vlambda import schema

BACKUP_FORMAT = "vlambda-backup/1"
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".pytest_cache")
_SIDECARS = ("-wal", "-shm")


class BackupError(RuntimeError):
    """The backup or restore could not be completed safely."""


def _default_db() -> str:
    return os.environ.get("VLAMBDA_DB", "./vlambda.db")


def _default_metrics_db() -> str:
    return os.environ.get("VLAMBDA_METRICS_DB", _default_db().replace(".db", "_metrics.db"))


def _default_projects() -> str:
    return os.environ.get("VLAMBDA_PROJECTS_DIR", "/tmp/opencode/vlambda_projects")


def _default_packages() -> str:
    return os.environ.get("VLAMBDA_PACKAGES_DIR", "/tmp/opencode/vlambda_packages")


def _package_version() -> str:
    from vlambda.version import __version__

    return __version__


def _key_fingerprint() -> str:
    raw = os.environ.get("VLAMBDA_ENCRYPTION_KEY") or os.environ.get("VLAMBDA_SECRET", "")
    if not raw:
        return ""
    return hmac.new(raw.encode(), b"vlambda-backup-key", hashlib.sha256).hexdigest()[:16]


def _connect_ro(path: str) -> sqlite3.Connection:
    uri = "file:" + quote(Path(path).resolve().as_posix()) + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=15)


def _sqlite_backup(src_path: str, dst_path: str) -> None:
    if not os.path.exists(src_path):
        raise BackupError(f"database not found: {src_path}")
    dst = sqlite3.connect(dst_path)
    try:
        with closing(_connect_ro(src_path)) as src:
            src.backup(dst)
    except sqlite3.Error as exc:
        raise BackupError(f"could not back up {src_path}: {exc}") from exc
    finally:
        dst.close()


def _integrity(path: str) -> None:
    try:
        with closing(_connect_ro(path)) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        raise BackupError(f"{path}: cannot read database: {exc}") from exc
    result = row[0] if row else "no result"
    if result != "ok":
        raise BackupError(f"{path}: integrity check failed: {result}")


def _tar_filter(info: tarfile.TarInfo):
    name = info.name
    if "__pycache__" in name or name.endswith(".pyc") or "/.git" in name:
        return None
    return info


def create_backup(
    out_path: str,
    *,
    db_path: str | None = None,
    metrics_db: str | None = None,
    projects_dir: str | None = None,
    packages_dir: str | None = None,
    include_packages: bool = False,
) -> dict[str, Any]:
    """Write a backup archive and return its manifest."""
    db_path = db_path or _default_db()
    metrics_db = metrics_db or _default_metrics_db()
    projects_dir = projects_dir or _default_projects()
    packages_dir = packages_dir or _default_packages()

    db_version = schema.require(db_path)
    metrics_version = 0
    if os.path.exists(metrics_db):
        metrics_version = schema.check(metrics_db, schema.METRICS_SCHEMA_VERSION)

    manifest: dict[str, Any] = {
        "format": BACKUP_FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "vlambda_version": _package_version(),
        "schema_version": db_version,
        "metrics_schema_version": metrics_version or None,
        "encryption_key_fingerprint": _key_fingerprint(),
        "include_packages": bool(include_packages),
        "contents": ["db/vlambda.db"],
    }
    if metrics_version:
        manifest["contents"].append("db/metrics.db")
    if os.path.isdir(projects_dir):
        manifest["contents"].append("projects")
    if include_packages and os.path.isdir(packages_dir):
        manifest["contents"].append("packages")

    out = Path(out_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_archive = out.with_name(out.name + ".tmp")
    with tempfile.TemporaryDirectory(prefix="vlambda-backup-") as tmp:
        root = Path(tmp)
        (root / "db").mkdir()
        _sqlite_backup(db_path, str(root / "db" / "vlambda.db"))
        _integrity(str(root / "db" / "vlambda.db"))
        if metrics_version:
            _sqlite_backup(metrics_db, str(root / "db" / "metrics.db"))
            _integrity(str(root / "db" / "metrics.db"))
        if "projects" in manifest["contents"]:
            shutil.copytree(projects_dir, root / "projects", ignore=_IGNORE)
        if "packages" in manifest["contents"]:
            shutil.copytree(packages_dir, root / "packages", ignore=_IGNORE)
        (root / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        try:
            with tarfile.open(tmp_archive, "w:gz") as tar:
                tar.add(root / "manifest.json", arcname="manifest.json")
                for entry in sorted(manifest["contents"]):
                    tar.add(root / entry, arcname=entry, filter=_tar_filter)
            os.replace(tmp_archive, out)
        except Exception:
            try:
                tmp_archive.unlink()
            except FileNotFoundError:
                pass
            raise

    return manifest


def _read_manifest(root: Path) -> dict[str, Any]:
    path = root / "manifest.json"
    if not path.exists():
        raise BackupError("archive has no manifest.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BackupError(f"invalid manifest: {exc}") from exc
    if data.get("format") != BACKUP_FORMAT:
        raise BackupError(f"unsupported backup format: {data.get('format')!r}")
    return data


def _move_aside(path: str) -> str:
    stamp = time.strftime("%Y%m%d%H%M%S")
    dest = f"{path}.bak-{stamp}"
    counter = 1
    while os.path.exists(dest):
        dest = f"{path}.bak-{stamp}-{counter}"
        counter += 1
    os.replace(path, dest)
    for suffix in _SIDECARS:
        side = path + suffix
        if os.path.exists(side):
            os.replace(side, dest + suffix)
    return dest


def _require_target(path: str, force: bool) -> str | None:
    if not os.path.exists(path):
        return None
    if not force:
        raise BackupError(f"{path} already exists; pass --force to replace it")
    return _move_aside(path)


def restore_backup(
    archive: str,
    *,
    db_path: str | None = None,
    metrics_db: str | None = None,
    projects_dir: str | None = None,
    packages_dir: str | None = None,
    force: bool = False,
    allow_key_mismatch: bool = False,
) -> dict[str, Any]:
    """Restore a backup archive into the configured paths."""
    db_path = db_path or _default_db()
    metrics_db = metrics_db or _default_metrics_db()
    projects_dir = projects_dir or _default_projects()
    packages_dir = packages_dir or _default_packages()

    with tempfile.TemporaryDirectory(prefix="vlambda-restore-") as tmp:
        root = Path(tmp)
        try:
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(root, filter="data")
        except (tarfile.TarError, OSError) as exc:
            raise BackupError(f"cannot extract {archive}: {exc}") from exc

        manifest = _read_manifest(root)
        db_version = int(manifest.get("schema_version") or 0)
        metrics_version = int(manifest.get("metrics_schema_version") or 0)
        if db_version < 1 or db_version > schema.SCHEMA_VERSION:
            raise BackupError(
                f"backup schema version {db_version} is not supported by this build "
                f"({schema.SCHEMA_VERSION})"
            )
        if metrics_version > schema.METRICS_SCHEMA_VERSION:
            raise BackupError(
                f"backup metrics schema version {metrics_version} is not supported by this build "
                f"({schema.METRICS_SCHEMA_VERSION})"
            )

        backup_key = str(manifest.get("encryption_key_fingerprint") or "")
        current_key = _key_fingerprint()
        key_mismatch = bool(backup_key and current_key and backup_key != current_key)
        if key_mismatch and not allow_key_mismatch:
            raise BackupError(
                "backup was made with a different VLAMBDA_SECRET/ VLAMBDA_ENCRYPTION_KEY; "
                "encrypted secrets will not decrypt. Pass --allow-key-mismatch to restore anyway."
            )

        src_db = root / "db" / "vlambda.db"
        if not src_db.exists():
            raise BackupError("archive has no db/vlambda.db")
        _integrity(str(src_db))

        src_metrics = root / "db" / "metrics.db"
        has_metrics = src_metrics.exists()
        if has_metrics:
            _integrity(str(src_metrics))

        src_projects = root / "projects"
        src_packages = root / "packages"
        has_projects = src_projects.exists()
        has_packages = src_packages.exists()

        moved: list[str] = []
        moved_db = _require_target(db_path, force)
        if moved_db:
            moved.append(moved_db)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_db), db_path)

        if has_metrics:
            moved_metrics = _require_target(metrics_db, force)
            if moved_metrics:
                moved.append(moved_metrics)
            Path(metrics_db).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_metrics), metrics_db)

        for source, target in ((src_projects, projects_dir), (src_packages, packages_dir)):
            if not source.exists():
                continue
            moved_target = _require_target(target, force)
            if moved_target:
                moved.append(moved_target)
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), target)

    return {
        "restored": True,
        "db": db_path,
        "metrics_db": metrics_db if has_metrics else None,
        "projects_dir": projects_dir if has_projects else None,
        "packages_dir": packages_dir if has_packages else None,
        "schema_version": db_version,
        "metrics_schema_version": metrics_version or None,
        "key_mismatch": key_mismatch,
        "replaced": moved,
    }
