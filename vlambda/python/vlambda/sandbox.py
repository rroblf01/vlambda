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
    "os", "subprocess", "sys", "importlib", "ctypes", "socket",
    "urllib", "http", "shutil", "pathlib", "io", "pickle", "marshal",
    "code", "codeop", "builtins", "importlib", "runpy",
}


def blocked_import(name, *args, **kwargs):
    base = name.split(".")[0]
    if base in BLOCKED_MODULES:
        raise ImportError(f"module '{name}' is not allowed")
    # Allow safe modules
    import importlib
    return importlib.import_module(name)


def make_sandbox(code: str, request_obj):
    """Compile and return (code_obj, namespace) for safe execution."""
    ns = {"__builtins__": {k: getattr(builtins, k) for k in SAFE_BUILTINS}}
    ns["__builtins__"]["__import__"] = blocked_import
    ns["request"] = request_obj
    compiled = compile(code, "<lambda>", "exec")
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
