"""Keeping secrets out of anything that is stored, logged or shown.

Two mechanisms, both applied to tool arguments and tool results:

- exact values: every secret a session was given is replaced wherever it
  appears, in plain and in the common encoded forms;
- shapes: bearer tokens, JSON web tokens, cookie headers and sensitive URL
  parameters are masked even when the session never knew the value.
"""

import html
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit

MASK = "[REDACTED]"

_SENSITIVE_PARAMS = re.compile(
    r"(pass(word|wd)?|secret|token|key|code|session|sid|auth|signature|sig|otp|pin)", re.I
)
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.I), rf"\1 {MASK}"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), MASK),
    (
        re.compile(r"(?im)^(\s*(?:set-cookie|cookie|authorization|proxy-authorization)\s*:\s*).+$"),
        rf"\1{MASK}",
    ),
)
_URL = re.compile(r"https?://[^\s\"'<>)\]]+")


def sanitize_url(url: str) -> str:
    """Drop credentials in the address and mask the values of sensitive parameters."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return MASK
    if not parts.scheme or not parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[-1]
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        if any(_SENSITIVE_PARAMS.search(name) for name, _ in pairs):
            query = urlencode(
                [(n, MASK if _SENSITIVE_PARAMS.search(n) else v) for n, v in pairs], safe="[]"
            )
    fragment = MASK if "=" in parts.fragment else parts.fragment
    return urlunsplit((parts.scheme, host, parts.path, query, fragment))


class Redactor:
    """Removes the secrets one session knows about, and anything shaped like one."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = tuple(dict.fromkeys(secret for secret in secrets if secret))
        variants: set[str] = set()
        for secret in self._secrets:
            variants.update(
                {
                    secret,
                    quote(secret, safe=""),
                    quote_plus(secret),
                    html.escape(secret),
                    json.dumps(secret)[1:-1],
                }
            )
        # Longest first, so a secret that contains another is removed whole.
        self._variants = sorted(variants, key=len, reverse=True)

    def extended(self, *secrets: str) -> "Redactor":
        """A redactor that also knows these secrets."""
        return Redactor([*self._secrets, *secrets])

    def text(self, value: str) -> str:
        for variant in self._variants:
            if variant in value:
                value = value.replace(variant, MASK)
        for pattern, replacement in _PATTERNS:
            value = pattern.sub(replacement, value)
        return _URL.sub(lambda match: sanitize_url(match.group(0)), value)

    def value(self, value: Any) -> Any:
        """Redact every string inside a JSON-like structure, keys included."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {self.text(str(key)): self.value(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self.value(item) for item in value]
        return value

    def arguments(self, arguments: Mapping[str, Any], *, max_chars: int = 300) -> dict[str, Any]:
        """Tool arguments in a form that is safe and small enough to store."""
        redacted = self.value(dict(arguments))
        assert isinstance(redacted, dict)  # noqa: S101
        return {str(key): _shorten(item, max_chars) for key, item in redacted.items()}

    def summary(self, text: str, *, max_chars: int = 600) -> str:
        return _shorten(self.text(text), max_chars)  # type: ignore[no-any-return]


def _shorten(value: Any, max_chars: int) -> Any:
    if isinstance(value, str):
        return (
            value
            if len(value) <= max_chars
            else value[:max_chars] + f"… (+{len(value) - max_chars} chars)"
        )
    if isinstance(value, list):
        return [_shorten(item, max_chars) for item in value[:20]]
    if isinstance(value, dict):
        return {key: _shorten(item, max_chars) for key, item in list(value.items())[:20]}
    return value
