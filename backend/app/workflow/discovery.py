"""A read-only look at the application through Playwright MCP.

Used before test cases are written, so that scripts can refer to elements that
exist. The session is granted only the tools that navigate and read; it cannot
click, type or submit. Which pages are opened is decided by this code, in a
fixed, bounded order: the pages asked for, then same-site links found on them.
No model takes part, so nothing written on a page can steer the visit.

What is kept is an outline: each page's address and title, and the roles and
names of its elements. Page text is not kept.
"""

import re
import uuid
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from app.browser.errors import BrowserError
from app.browser.events import Recorder, SessionEvent, ToolCallEvent
from app.browser.manager import BrowserSessionManager, SessionSpec
from app.browser.policy import SessionGrant
from app.browser.services import LaunchOptions
from app.browser.session import BrowserSession
from app.core.security.url_safety import DestinationPolicy
from app.workflow.state import PageSummary, ToolResultSummary

DISCOVERY_TOOLS = frozenset({"browser_navigate", "browser_snapshot"})
MAX_PAGES = 6
MAX_ELEMENTS = 80
_ROLES = frozenset(
    {
        "alert", "button", "checkbox", "combobox", "heading", "link", "listbox", "menuitem",
        "option", "radio", "searchbox", "slider", "spinbutton", "status", "switch", "tab",
        "table", "textbox",
    }
)  # fmt: skip
_ELEMENT = re.compile(r'^\s*- ([a-z]+) "((?:[^"\\]|\\.)*)"', re.M)
_LINK_TARGET = re.compile(r"^\s*- /url: (\S+)\s*$", re.M)
# Links whose very purpose is to change something are not followed, even with GET.
_SKIP = re.compile(r"log-?out|sign-?out|delete|remove|unsubscribe|cancel", re.I)


@dataclass(slots=True)
class Discovery:
    inspected_with: str
    reachable: bool
    pages: list[PageSummary] = field(default_factory=list)
    tool_results: list[ToolResultSummary] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class _Collecting:
    """Passes events on and keeps a compact copy for the workflow state."""

    def __init__(self, inner: Recorder, results: list[ToolResultSummary]) -> None:
        self._inner = inner
        self._results = results

    async def tool_call(self, event: ToolCallEvent) -> None:
        self._results.append(
            ToolResultSummary(
                tool=event.tool,
                status=event.status,
                page_url=event.page_url,
                error_code=event.error_code,
            )
        )
        await self._inner.tool_call(event)

    async def session(self, event: SessionEvent) -> None:
        await self._inner.session(event)


def outline(snapshot: str) -> list[list[str]]:
    """(role, name) of the elements in an accessibility snapshot that a test could use."""
    seen: dict[tuple[str, str], None] = {}
    for role, name in _ELEMENT.findall(snapshot):
        if role in _ROLES and name.strip():
            seen.setdefault((role, name.replace('\\"', '"')[:120]), None)
            if len(seen) >= MAX_ELEMENTS:
                break
    return [[role, name] for role, name in seen]


def same_site_links(snapshot: str, page_url: str) -> list[str]:
    """Addresses of links on the page that stay on the same origin."""
    origin = urlsplit(page_url)
    found: dict[str, None] = {}
    for target in _LINK_TARGET.findall(snapshot):
        absolute = urlsplit(urljoin(page_url, target))
        if (absolute.scheme, absolute.netloc) != (origin.scheme, origin.netloc):
            continue
        if _SKIP.search(absolute.path) or _SKIP.search(absolute.query):
            continue
        found.setdefault(f"{absolute.scheme}://{absolute.netloc}{absolute.path or '/'}", None)
    return list(found)


async def discover(
    manager: BrowserSessionManager,
    *,
    organization_id: uuid.UUID,
    destinations: DestinationPolicy,
    allowed_actions: frozenset[str],
    base_url: str,
    paths: list[str],
    options: LaunchOptions,
    recorder: Recorder,
    max_pages: int = MAX_PAGES,
) -> Discovery:
    result = Discovery(inspected_with="mcp_read_only", reachable=False)
    spec = SessionSpec(
        organization_id=organization_id,
        destinations=destinations,
        # Whatever the environment allows, discovery only navigates and reads.
        grant=SessionGrant(
            allowed_actions=allowed_actions & {"navigate", "read"},
            read_only=True,
            requested_tools=DISCOVERY_TOOLS,
        ),
        options=options,
    )
    base = urlsplit(base_url)
    origin = f"{base.scheme}://{base.netloc}"
    queue = [base_url, *(origin + path for path in paths if path.startswith("/"))]
    visited: set[str] = set()
    try:
        async with manager.open(spec, _Collecting(recorder, result.tool_results)) as browser:
            while queue and len(result.pages) < max_pages:
                url = queue.pop(0)
                key = url.rstrip("/")
                if key in visited:
                    continue
                visited.add(key)
                first = not result.pages and not result.reachable and len(visited) == 1
                try:
                    page = await _look(browser, url)
                except BrowserError as exc:
                    result.notes.append(f"{urlsplit(url).path or '/'}: {exc.code}")
                    if first or browser.state != "open":
                        # The application's own address could not be opened, or the
                        # session ended: there is nothing more to learn here.
                        break
                    continue
                result.reachable = True
                result.pages.append(page[0])
                queue.extend(link for link in page[1] if link.rstrip("/") not in visited)
    except BrowserError as exc:
        if not result.pages:
            result.inspected_with = f"unavailable:{exc.code}"
        result.notes.append(f"browser: {exc.code}")
    return result


async def _look(browser: BrowserSession, url: str) -> tuple[PageSummary, list[str]]:
    opened = await browser.call("browser_navigate", {"url": url})
    snapshot = await browser.call("browser_snapshot")
    page_url = snapshot.page_url or opened.page_url or url
    return (
        PageSummary(
            url=page_url,
            title=snapshot.page_title or opened.page_title,
            elements=outline(snapshot.text),
        ),
        same_site_links(snapshot.text, page_url),
    )
