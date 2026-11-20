"""Clients for the two services a browser session depends on.

- the runner starts and stops one Playwright MCP server (and so one browser)
  per session;
- the egress proxy is the only way out of the runner's network, and is told
  which origins each session may reach.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.browser.errors import BrowserLaunchError, RunnerBusyError, RunnerUnavailableError
from app.core.security.url_safety import DestinationPolicy


@dataclass(frozen=True, slots=True)
class Viewport:
    width: int = 1280
    height: int = 720


@dataclass(frozen=True, slots=True)
class LaunchOptions:
    browser: str = "chromium"
    viewport: Viewport = field(default_factory=Viewport)
    action_timeout_ms: int = 5_000
    navigation_timeout_ms: int = 30_000
    ttl_s: int = 600


@dataclass(frozen=True, slots=True)
class RunnerSession:
    session_id: uuid.UUID
    mcp_url: str


class RunnerClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_s: float = 40.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._host = urlsplit(self._base_url).hostname or ""
        self._headers = {"Authorization": f"Bearer {token}"}
        self._timeout_s = timeout_s
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=self._timeout_s,
            transport=self._transport,
            trust_env=False,
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            async with self._client() as client:
                return await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RunnerUnavailableError(
                f"The browser runner could not be reached ({type(exc).__name__})."
            ) from None

    async def create(
        self,
        session_id: uuid.UUID,
        *,
        options: LaunchOptions,
        allowed_origins: list[str],
        proxy_url: str | None,
    ) -> RunnerSession:
        response = await self._request(
            "POST",
            "/sessions",
            json={
                "session_id": str(session_id),
                "browser": options.browser,
                "viewport": {"width": options.viewport.width, "height": options.viewport.height},
                "allowed_origins": allowed_origins,
                "proxy_url": proxy_url,
                "action_timeout_ms": options.action_timeout_ms,
                "navigation_timeout_ms": options.navigation_timeout_ms,
                "ttl_s": options.ttl_s,
            },
        )
        if response.status_code == 429:
            raise RunnerBusyError("All browser slots are in use. Try again shortly.")
        if response.status_code == 422:
            raise BrowserLaunchError(_detail(response, "The runner rejected the browser settings."))
        if response.status_code != 201:
            raise RunnerUnavailableError(
                _detail(response, f"The browser runner answered {response.status_code}.")
            )
        try:
            port = int(response.json()["port"])
        except (ValueError, KeyError, TypeError):
            raise RunnerUnavailableError("The browser runner sent an unusable answer.") from None
        # The host is ours, not the runner's to choose: only the port is taken from it.
        return RunnerSession(session_id, f"http://{self._host}:{port}/mcp")

    async def delete(self, session_id: uuid.UUID) -> None:
        response = await self._request("DELETE", f"/sessions/{session_id}")
        if response.status_code not in (204, 404):
            raise RunnerUnavailableError(f"The browser runner answered {response.status_code}.")

    async def health(self) -> dict[str, Any]:
        response = await self._request("GET", "/healthz")
        if response.status_code != 200:
            raise RunnerUnavailableError(f"The browser runner answered {response.status_code}.")
        body = response.json()
        return body if isinstance(body, dict) else {}


def _detail(response: httpx.Response, fallback: str) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return fallback
    return str(detail)[:300] if detail else fallback


class EgressClient:
    """Tells the egress proxy what one session may reach."""

    def __init__(
        self,
        admin_url: str,
        token: str,
        proxy_url: str,
        *,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._admin_url = admin_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._proxy = urlsplit(proxy_url)
        self._timeout_s = timeout_s
        self._transport = transport

    def proxy_url_for(self, session_id: uuid.UUID, secret: str) -> str:
        """Proxy address carrying this session's own credentials."""
        return f"{self._proxy.scheme}://{session_id}:{secret}@{self._proxy.netloc}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                base_url=self._admin_url,
                headers=self._headers,
                timeout=self._timeout_s,
                transport=self._transport,
                trust_env=False,
            ) as client:
                return await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RunnerUnavailableError(
                f"The egress proxy could not be reached ({type(exc).__name__})."
            ) from None

    async def register(
        self, session_id: uuid.UUID, secret: str, policy: DestinationPolicy, *, ttl_s: int
    ) -> None:
        response = await self._request(
            "PUT",
            f"/sessions/{session_id}",
            json={
                "secret": secret,
                "allowed_origins": sorted(str(origin) for origin in policy.allowed_origins),
                "allow_private_network": policy.allow_private_network,
                "allow_http": policy.allow_http,
                "ttl_s": ttl_s,
            },
        )
        if response.status_code != 204:
            raise RunnerUnavailableError(f"The egress proxy answered {response.status_code}.")

    async def unregister(self, session_id: uuid.UUID) -> list[dict[str, Any]]:
        """Remove the session's rules. Returns what the proxy refused on its behalf."""
        response = await self._request("DELETE", f"/sessions/{session_id}")
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            raise RunnerUnavailableError(f"The egress proxy answered {response.status_code}.")
        blocked = response.json().get("blocked", [])
        return blocked if isinstance(blocked, list) else []
