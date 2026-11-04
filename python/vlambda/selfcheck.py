"""Startup checks for directories and the requested sandbox backend."""

from __future__ import annotations

import ctypes.util
import os
import tempfile
from pathlib import Path


class SelfCheckError(RuntimeError):
    """A startup requirement is not satisfied."""


def check_writable(path: str, label: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".vlambda-check-", dir=path)
        os.close(fd)
        os.remove(probe)
    except OSError as exc:
        raise SelfCheckError(f"{label} is not writable ({path}): {exc}") from exc


def check_sandbox(mode: str) -> None:
    mode = (mode or "").lower()
    if mode not in ("fork", "forklandlock", "landlock"):
        return
    try:
        from vlambda.sandbox_os import sandbox_supported
    except Exception as exc:
        raise SelfCheckError(f"cannot load the sandbox backend: {exc}") from exc
    if not sandbox_supported():
        raise SelfCheckError(
            "Landlock is not available; use a Linux kernel with Landlock (5.13+) "
            "or set VLAMBDA_SANDBOX=process for development"
        )
    if not ctypes.util.find_library("seccomp"):
        raise SelfCheckError(
            "libseccomp is not available; install libseccomp2 or use another sandbox mode"
        )


def run(projects_dir: str, packages_dir: str, cache_dir: str, sandbox_mode: str = "") -> None:
    check_writable(projects_dir, "projects directory")
    check_writable(packages_dir, "packages directory")
    check_writable(cache_dir, "cache directory")
    check_sandbox(sandbox_mode)
