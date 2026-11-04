"""Database schema versions used by backups and upgrade checks."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing

# Bump these when the schema changes.  A backup is only restorable by a build
# that knows its version or can migrate it forward.
SCHEMA_VERSION = 1
METRICS_SCHEMA_VERSION = 1


class SchemaError(RuntimeError):
    """The database schema is missing, newer or otherwise unusable."""


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=15)
    mode = os.environ.get("VLAMBDA_SQLITE_JOURNAL", "wal").upper()
    if mode not in ("WAL", "DELETE"):
        mode = "WAL"
    conn.execute(f"PRAGMA journal_mode={mode}")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def version(path: str) -> int:
    """Return ``PRAGMA user_version`` for a database, or 0 if it is absent."""
    if not os.path.exists(path):
        return 0
    with closing(_connect(path)) as conn:
        row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0] or 0) if row else 0


def check(path: str, current: int = SCHEMA_VERSION) -> int:
    """Validate a database version against the running build."""
    found = version(path)
    if found > current:
        raise SchemaError(
            f"{path}: schema version {found} is newer than this vlambda build "
            f"({current}); upgrade vlambda or restore an older backup"
        )
    return found


def require(path: str, current: int = SCHEMA_VERSION) -> int:
    """Validate a database and require that it has been initialized."""
    found = check(path, current)
    if found == 0:
        raise SchemaError(
            f"{path}: database has no schema version; start vlambda once to initialize it"
        )
    return found
