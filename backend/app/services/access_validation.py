"""Reachability probe for an environment.

Sends one GET to the environment's base URL and follows redirects by hand, so
that every hop goes through `AccessValidator.validate`. Each connection is made
to the IP address that was just validated ("pinned"), with the original host
name sent for TLS and the Host header, so a DNS change between the check and
the connection cannot redirect the request somewhere else.

Only the status line and headers are read. Response bodies are never returned
to the caller: a probe must not become a way to read internal pages.
"""

import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx

from app.core.security.url_safety import (
    AccessValidator,
    DestinationPolicy,
    UrlNotAllowedError,
    ValidatedDestination,
)

logger = logging.getLogger(__name__)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_USER_AGENT = "qa-agent-access-check/1.0"


@dataclass(frozen=True, slots=True)
class ProbeHop:
    url: str
    status: int | None
    blocked_code: str | None = None
    blocked_reason: str | None = None


@dataclass(slots=True)
class ProbeResult:
    reachable: bool
    final_url: str | None = None
    status: int | None = None
    duration_ms: int = 0
    error_code: str | None = None
    error: str | None = None
    hops: list[ProbeHop] = field(default_factory=list)


def _pinned_request(client: httpx.AsyncClient, destination: ValidatedDestination) -> httpx.Request:
    origin = destination.origin
    address = destination.addresses[0]
    ip_host = f"[{address}]" if ":" in address else address
    return client.build_request(
        "GET",
        f"{origin.scheme}://{ip_host}:{origin.port}{destination.path_and_query}",
        headers={"Host": origin.netloc, "User-Agent": _USER_AGENT, "Accept": "*/*"},
        # TLS: present and verify the certificate for the real host name, not the IP.
        extensions={"sni_hostname": origin.host},
    )


class AccessProbe:
    def __init__(
        self,
        validator: AccessValidator,
        *,
        timeout_s: float = 5.0,
        max_redirects: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._validator = validator
        self._timeout_s = timeout_s
        self._max_redirects = max_redirects
        self._transport = transport

    async def probe(self, url: str, policy: DestinationPolicy) -> ProbeResult:
        result = ProbeResult(reachable=False)
        started = time.perf_counter()
        current = url
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout_s,
                follow_redirects=False,  # redirects are followed below, one validated hop at a time
                trust_env=False,  # ignore proxy settings from the environment
            ) as client:
                for _ in range(self._max_redirects + 1):
                    destination = await self._validator.validate(current, policy)
                    response = await client.send(_pinned_request(client, destination), stream=True)
                    await response.aclose()  # the body is deliberately not read
                    result.hops.append(ProbeHop(url=current, status=response.status_code))

                    location = response.headers.get("location")
                    if response.status_code in _REDIRECT_STATUSES and location:
                        current = urljoin(current, location)
                        continue
                    result.reachable = response.status_code < 500
                    result.final_url = current
                    result.status = response.status_code
                    if not result.reachable:
                        result.error_code = "server_error"
                        result.error = (
                            f"The application answered with status {response.status_code}."
                        )
                    break
                else:
                    result.error_code = "too_many_redirects"
                    result.error = f"More than {self._max_redirects} redirects."
        except UrlNotAllowedError as exc:
            # Either the first URL or a redirect target is outside what is allowed.
            result.hops.append(
                ProbeHop(
                    url=current, status=None, blocked_code=exc.code, blocked_reason=exc.message
                )
            )
            result.error_code = exc.code
            result.error = exc.message
        except httpx.TimeoutException:
            result.error_code = "timeout"
            result.error = "The application did not answer in time."
        except httpx.HTTPError as exc:
            logger.info("access_probe_failed", extra={"error_type": type(exc).__name__})
            result.error_code = "connection_failed"
            result.error = "Could not connect to the application."
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result
