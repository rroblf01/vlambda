"""Security sandbox for executing user lambda code."""

from __future__ import annotations

import builtins

SAFE_BUILTINS = {
    "abs", "all", "any", "bool", "dict", "enumerate", "filter",
    "float", "frozenset", "int", "len", "list", "map", "max", "min",
    "range", "reversed", "round", "set", "sorted", "str", "sum",
    "tuple", "zip", "isinstance", "issubclass", "type", "print",
    "chr", "ord", "hex", "bin", "oct", "repr", "format",
}

BLOCKED_MODULES = {
    "os", "posix", "nt", "subprocess", "_posixsubprocess", "sys", "importlib", "ctypes", "_ctypes",
    "socket", "_socket", "ssl", "_ssl", "urllib", "http", "shutil", "pathlib", "io", "pickle", "marshal",
    "code", "codeop", "builtins", "runpy", "gc", "resource", "fcntl", "mmap",
    "signal", "threading", "multiprocessing", "concurrent", "asyncio",
    "pwd", "grp", "spwd", "pty", "termios", "tempfile", "glob",
    "sqlite3", "dbm", "shelve", "sysconfig", "site", "platform",
}


def blocked_import(name, *args, **kwargs):
    base = name.split(".")[0]
    if base in BLOCKED_MODULES:
        raise ImportError(f"module '{name}' is not allowed")
    # Allow safe modules
    import importlib
    return importlib.import_module(name)


# Pre-imported modules (loaded once, shared across sandboxes)
# Modules are imported on demand by blocked_import. Preloading them at module
# import makes every fresh sandbox process pay for modules the lambda may not use.
_PREIMPORTED = {}




def make_namespace(request_obj=None):
    """Build a fresh sandbox namespace without compiling code."""
    ns = {"__builtins__": {k: getattr(builtins, k) for k in SAFE_BUILTINS}}
    ns["__builtins__"]["__import__"] = blocked_import
    ns.update(_PREIMPORTED)
    ns["request"] = request_obj
    return ns


def make_sandbox(code: str, request_obj):
    """Compile and return (code_obj, namespace) for safe execution."""
    ns = make_namespace(request_obj)
    compiled = compile(code, "<lambda>", "exec", optimize=2)
    return compiled, ns


def execute_lambda(code: str, request_obj) -> dict:
    """Execute lambda code and return the result."""
    compiled, ns = make_sandbox(code, request_obj)
    exec(compiled, ns)
    handler = ns.get("handler")
    if handler is None:
        raise ValueError("no 'handler' function defined")
    result = handler(request_obj)
    return result
