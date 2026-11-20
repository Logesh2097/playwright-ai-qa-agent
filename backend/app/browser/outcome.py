"""Reading what Playwright MCP puts in a tool result.

The server answers in Markdown with `### ` sections ("Page", "Error", "Events",
"Modal state", ...). That layout is observed behaviour of version 0.0.86, not a
documented contract, so everything here is best-effort: a section that is
missing or renamed yields "unknown" (None), never a guess. Callers must not
treat "unknown" as "fine".
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.mcp.results import ImageBlock, RawToolResult

_HEADING = re.compile(r"^### +(.+?)\s*$", re.M)
_PAGE_URL = re.compile(r"^- Page URL: (.+?)\s*$", re.M)
_PAGE_TITLE = re.compile(r"^- Page Title: (.*?)\s*$", re.M)
_DOWNLOAD = re.compile(r"^- Download(?:ing|ed) file (.+?)(?: \.\.\.| to .*)?\s*$", re.M)


@dataclass(frozen=True, slots=True)
class Reading:
    """Facts extracted from one result. None means the result did not say."""

    is_error: bool
    error_text: str | None
    page_url: str | None
    page_title: str | None
    dialog_open: bool
    downloads: tuple[str, ...]
    sections: Mapping[str, str]
    text: str
    images: tuple[ImageBlock, ...]


def read_result(raw: RawToolResult) -> Reading:
    text = raw.text
    sections = _sections(text)
    page = sections.get("Page", "")
    url = _PAGE_URL.search(page)
    title = _PAGE_TITLE.search(page)
    error_text = sections.get("Error") if raw.is_error else None
    if raw.is_error and error_text is None:
        error_text = text.strip() or "The tool reported an error without a message."
    return Reading(
        is_error=raw.is_error,
        error_text=error_text,
        page_url=url.group(1) if url else None,
        page_title=title.group(1) if title else None,
        dialog_open="Modal state" in sections,
        downloads=tuple(dict.fromkeys(_DOWNLOAD.findall(sections.get("Events", "")))),
        sections=MappingProxyType(sections),
        text=text,
        images=raw.images,
    )


def _sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    matches = list(_HEADING.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.setdefault(match.group(1), text[match.end() : end].strip())
    return sections


def classify_error(error_text: str) -> str:
    """Map the server's error text to one of our codes. Unrecognised text is `tool_failed`."""
    lowered = error_text.lower()
    if "failed to launch" in lowered or "browsertype.launch" in lowered:
        return "browser_launch_failed"
    if "timeouterror" in lowered or re.search(r"timeout \d+ms exceeded", lowered):
        return "browser_timeout"
    if (
        "target page, context or browser has been closed" in lowered
        or "browser has been closed" in lowered
    ):
        return "browser_closed"
    if (
        "err_blocked_by_client" in lowered
        or "err_tunnel_connection_failed" in lowered
        or "err_proxy" in lowered
    ):
        return "destination_not_allowed"
    if (
        "net::err_" in lowered
        or "is interrupted by another navigation" in lowered
        or "protocol is blocked" in lowered
    ):
        return "navigation_failed"
    if lowered.startswith("invalid arguments") or "invalid input" in lowered:
        return "mcp_invalid_arguments"
    return "tool_failed"
