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


# Pre-imported modules (loaded once, shared across sandboxes)
_PREIMPORTED = {}
def _preload():
    import json, math, re, datetime, collections, itertools, functools
    import hashlib, base64, string, textwrap, operator
    import decimal, fractions, random, statistics
    _PREIMPORTED.update({
        "json": json, "math": math, "re": re,
        "datetime": datetime, "collections": collections,
        "itertools": itertools, "functools": functools,
        "hashlib": hashlib, "base64": base64, "string": string,
        "textwrap": textwrap, "operator": operator,
        "decimal": decimal, "fractions": fractions,
        "random": random, "statistics": statistics,
    })
_preload()


def make_sandbox(code: str, request_obj):
    """Compile and return (code_obj, namespace) for safe execution."""
    ns = {"__builtins__": {k: getattr(builtins, k) for k in SAFE_BUILTINS}}
    ns["__builtins__"]["__import__"] = blocked_import
    ns.update(_PREIMPORTED)
    ns["request"] = request_obj
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
