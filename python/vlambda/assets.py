"""Embedded and packaged assets for the vlambda UI."""

import importlib.resources
import mimetypes

_RESOURCE = importlib.resources.files("vlambda")


def _read_text(name: str) -> str:
    return (_RESOURCE / name).read_text(encoding="utf-8")


def _read_bytes(name: str) -> bytes | None:
    path = _RESOURCE / name
    if not path.is_file():
        return None
    return path.read_bytes()


UI_HTML = _read_text("ui.html")


def get_asset(name: str) -> bytes | None:
    # Only serve files from the packaged static directory.
    if "/" in name or "\\" in name or name.startswith("."):
        return None
    return _read_bytes("static/" + name)


def asset_content_type(name: str) -> str:
    ctype, _ = mimetypes.guess_type(name)
    return ctype or "application/octet-stream"


FAVICON = _read_bytes("favicon.ico")
