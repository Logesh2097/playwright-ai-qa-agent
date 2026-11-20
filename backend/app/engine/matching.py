"""Comparing text. Pure functions: no browser, no I/O."""

import re
from typing import Any
from urllib.parse import urlsplit

from app.testing.automation import TextMatch

_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def squash(text: str) -> str:
    """Runs of white space count as one space, as they are displayed."""
    return " ".join(text.split())


def matches(match: TextMatch, expected: str, actual: Any) -> bool:
    """Does `actual` satisfy the comparison? `expected` is the rendered text of `match`."""
    if actual is None:
        return False
    text = str(actual)
    if match.kind == "matches":
        flags = re.IGNORECASE if match.ignore_case else 0
        try:
            return re.search(expected, text, flags) is not None
        except re.error:
            return False
    left, right = squash(text), squash(expected)
    if match.ignore_case:
        left, right = left.casefold(), right.casefold()
    if match.kind == "equals":
        return left == right
    if match.kind == "not_equals":
        return left != right
    return right in left


def url_subject(url: str, expected: str) -> str:
    """The part of an address a comparison is about.

    A full address is expected: compare the whole. A path with a query: path and
    query. A plain path: the path only, so tracking parameters do not matter.
    """
    if _SCHEME.match(expected):
        return url
    parts = urlsplit(url)
    path = parts.path or "/"
    return f"{path}?{parts.query}" if "?" in expected and parts.query else path


def json_pointer(document: Any, pointer: str) -> tuple[bool, Any]:
    """Resolve an RFC 6901 pointer. Returns (found, value)."""
    current = document
    if pointer == "":
        return True, current
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return False, None
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or int(token) >= len(current):
                return False, None
            current = current[int(token)]
        else:
            return False, None
    return True, current


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"
