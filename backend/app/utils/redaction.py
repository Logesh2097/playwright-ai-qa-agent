"""Removes secrets from data before it is logged or stored in the audit trail."""

import re
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"pass(word|wd)?|secret|token|authorization|cookie|api[_-]?key|private[_-]?key|"
    r"credential|ciphertext|\bdek\b|database_url|redis_url",
    re.IGNORECASE,
)


def is_sensitive_key(key: str) -> bool:
    return _SENSITIVE_KEY.search(key) is not None


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Return a copy of `value` with the values of sensitive keys replaced."""
    if _depth > 8:
        return REDACTED
    if isinstance(value, dict):
        return {
            str(key): REDACTED if is_sensitive_key(str(key)) else redact(item, _depth=_depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set):
        return [redact(item, _depth=_depth + 1) for item in value]
    return value
