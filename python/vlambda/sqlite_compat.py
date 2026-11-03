"""Choose a SQLite journal mode compatible with the V native extension.

The V extension uses the system ``libsqlite3`` while CPython may bundle a
different SQLite version. Mixing two SQLite libraries in WAL mode can unlink the
WAL file while other connections still have it open (seen on Python 3.15). When
the versions differ we fall back to the rollback journal, which is slower but
safe across libraries and still supports multi-process locking.
"""

from __future__ import annotations

import ctypes
import os
import sqlite3


def _system_version() -> str:
    try:
        lib = ctypes.CDLL("libsqlite3.so.0")
        lib.sqlite3_libversion.restype = ctypes.c_char_p
        return lib.sqlite3_libversion().decode("ascii", "replace")
    except Exception:
        return ""


def configure() -> str:
    # Sandboxed runners do not open SQLite; avoid leaking a VLAMBDA_* variable
    # into user code just because they import vlambda.
    if os.environ.get("VLRUN_NO_NATIVE") == "1":
        return os.environ.get("VLAMBDA_SQLITE_JOURNAL", "wal")
    configured = os.environ.get("VLAMBDA_SQLITE_JOURNAL", "").lower()
    if configured in ("wal", "delete"):
        return configured
    python_version = sqlite3.sqlite_version
    system_version = _system_version()
    mode = "wal" if not system_version or python_version == system_version else "delete"
    os.environ["VLAMBDA_SQLITE_JOURNAL"] = mode
    return mode
