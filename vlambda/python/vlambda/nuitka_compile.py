"""Optional Nuitka compilation for hot lambdas."""

from __future__ import annotations

import os
import subprocess
import sys
import importlib.util
import hashlib

_ENABLED = os.environ.get("VLAMBDA_NUITKA", "0") == "1"
_CACHE_DIR = os.environ.get("VLAMBDA_NUITKA_CACHE", "/tmp/opencode/vlambda_nuitka")


def _module_name(code: str) -> str:
    return "lambda_" + hashlib.sha256(code.encode()).hexdigest()[:12]


def compile_lambda(code: str):
    """Compile lambda code to a native module. Returns module or None."""
    if not _ENABLED:
        return None
    name = _module_name(code)
    so_path = os.path.join(_CACHE_DIR, name + ".so")
    if os.path.exists(so_path):
        spec = importlib.util.spec_from_file_location(name, so_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    os.makedirs(_CACHE_DIR, exist_ok=True)
    src_path = os.path.join(_CACHE_DIR, name + ".py")
    with open(src_path, "w") as f:
        f.write(code + "\n")
    try:
        subprocess.run(
            [sys.executable, "-m", "nuitka", "--module", src_path,
             f"--output-dir={_CACHE_DIR}", "--remove-output"],
            check=True, capture_output=True, timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    for fn in os.listdir(_CACHE_DIR):
        if fn.startswith(name) and fn.endswith(".so"):
            so_path = os.path.join(_CACHE_DIR, fn)
            spec = importlib.util.spec_from_file_location(name, so_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return None


def try_native(code: str, request):
    """Try to execute via native module. Returns result or None."""
    mod = compile_lambda(code)
    if mod is None:
        return None
    handler = getattr(mod, "handler", None)
    if handler is None:
        return None
    return handler(request)
