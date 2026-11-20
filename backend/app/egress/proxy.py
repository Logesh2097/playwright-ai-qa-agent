"""A forward proxy that only connects where a session is allowed to go.

Each browser session has its own proxy credentials and its own rules. For every
request (plain HTTP) or tunnel (CONNECT, used for HTTPS and WebSockets) the
proxy:

1. identifies the session from `Proxy-Authorization`;
2. checks that scheme, host and port are one of the session's allowed origins;
3. resolves the host itself and refuses if any address is forbidden;
4. connects to the address it just checked, never to a second lookup.

Because the browser can reach nothing except this proxy, the rules hold for
redirects, links, scripts, images and anything else a page does, not only for
addresses the platform passes to the browser.
"""

import asyncio
import base64
import binascii
import contextlib
import hmac
import ipaddress
import logging
import socket
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from app.core.security.url_safety import (
    AccessValidator,
    Origin,
    UrlNotAllowedError,
    normalize_host,
    parse_origin,
)
from app.egress.config import Network

logger = logging.getLogger(__name__)

_HEAD_LIMIT = 64 * 1024
_MAX_BLOCKED = 50
_HOP_HEADERS = frozenset({"proxy-authorization", "proxy-connection"})

Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


@dataclass(slots=True)
class SessionRules:
    secret: str
    allowed_origins: frozenset[Origin]
    allow_private_network: bool
    allow_http: bool
    expires_at: float
    blocked: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=_MAX_BLOCKED))
    allowed_count: int = 0
    blocked_count: int = 0


class RegistryFullError(Exception):
    pass


class SessionRegistry:
    def __init__(
        self, *, max_sessions: int = 200, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._sessions: dict[str, SessionRules] = {}
        self._max = max_sessions
        self._clock = clock

    def __len__(self) -> int:
        self._expire()
        return len(self._sessions)

    def _expire(self) -> None:
        now = self._clock()
        for session_id in [k for k, v in self._sessions.items() if v.expires_at <= now]:
            del self._sessions[session_id]

    def register(
        self,
        session_id: str,
        *,
        secret: str,
        allowed_origins: Iterable[Origin],
        allow_private_network: bool,
        allow_http: bool,
        ttl_s: float,
    ) -> None:
        self._expire()
        if session_id not in self._sessions and len(self._sessions) >= self._max:
            raise RegistryFullError
        self._sessions[session_id] = SessionRules(
            secret=secret,
            allowed_origins=frozenset(allowed_origins),
            allow_private_network=allow_private_network,
            allow_http=allow_http,
            expires_at=self._clock() + ttl_s,
        )

    def remove(self, session_id: str) -> SessionRules | None:
        return self._sessions.pop(session_id, None)

    def authenticate(self, session_id: str, secret: str) -> SessionRules | None:
        self._expire()
        rules = self._sessions.get(session_id)
        if rules is None or not hmac.compare_digest(rules.secret.encode(), secret.encode()):
            return None
        return rules


class _RefusedError(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def is_own_address(address: str) -> bool:
    """Does this address belong to the machine the proxy runs on?

    Binding a socket to an address only works for an address the machine has,
    which covers every interface without having to list them.
    """
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((address, 0))
    except OSError:
        return False
    return True


async def _open(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(host, port)


class EgressProxy:
    def __init__(
        self,
        registry: SessionRegistry,
        validator: AccessValidator,
        *,
        deny_networks: Iterable[Network] = (),
        connect_timeout_s: float = 10.0,
        idle_timeout_s: float = 300.0,
        connector: Connector = _open,
        own_address: Callable[[str], bool] = is_own_address,
    ) -> None:
        self._registry = registry
        self._validator = validator
        self._deny = tuple(deny_networks)
        self._connect_timeout_s = connect_timeout_s
        self._idle_timeout_s = idle_timeout_s
        self._connector = connector
        self._own_address = own_address

    async def serve(self, host: str, port: int) -> asyncio.Server:
        return await asyncio.start_server(self.handle, host, port, limit=_HEAD_LIMIT)

    # --- one client connection ---------------------------------------------------------

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream: asyncio.StreamWriter | None = None
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
                return
            try:
                method, target, headers = _parse_head(head)
                rules = self._authenticate(headers)
                origin, forward = _destination(method, target, headers)
                address = await self._permit(rules, method, origin)
            except _RefusedError as exc:
                if exc.status == 403 and not head.upper().startswith(b"CONNECT "):
                    # Answering a refused plain-HTTP request with an error page would
                    # put a page in the browser at the refused address, and a page that
                    # loaded looks like success. Ending the connection without an
                    # answer makes the browser report a failed navigation instead.
                    return
                await _respond(writer, exc.status, exc.reason)
                return
            try:
                up_reader, up_writer = await asyncio.wait_for(
                    self._connector(address, origin.port), timeout=self._connect_timeout_s
                )
            except (OSError, TimeoutError):
                await _respond(writer, 502, "upstream_unreachable")
                return
            upstream = up_writer
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            else:
                up_writer.write(forward)
                await up_writer.drain()
            await self._relay(reader, writer, up_reader, up_writer)
        except (ConnectionError, OSError):
            pass
        finally:
            for stream in (upstream, writer):
                if stream is not None:
                    stream.close()
                    with contextlib.suppress(ConnectionError, OSError):
                        await stream.wait_closed()

    def _authenticate(self, headers: list[tuple[str, str]]) -> SessionRules:
        value = next((v for k, v in headers if k.lower() == "proxy-authorization"), "")
        scheme, _, encoded = value.partition(" ")
        if scheme.lower() == "basic":
            try:
                user, _, secret = base64.b64decode(encoded, validate=True).decode().partition(":")
            except (binascii.Error, UnicodeDecodeError):
                user = secret = ""
            rules = self._registry.authenticate(user, secret) if user else None
            if rules is not None:
                return rules
        raise _RefusedError(407, "proxy_authentication_required")

    async def _permit(self, rules: SessionRules, method: str, origin: Origin) -> str:
        """Return the address to connect to, or refuse and note why."""
        try:
            candidates = [origin]
            if method == "CONNECT":
                # A tunnel does not say what will be spoken through it. WebSockets to a
                # plain-HTTP origin are tunnelled too, so both schemes are considered.
                candidates.append(Origin("http", origin.host, origin.port))
            permitted = [
                o
                for o in candidates
                if o in rules.allowed_origins and (o.scheme == "https" or rules.allow_http)
            ]
            if not permitted:
                raise _RefusedError(403, "origin_not_allowlisted")
            try:
                addresses = await self._validator.resolve_public(
                    permitted[0], allow_private=rules.allow_private_network
                )
            except UrlNotAllowedError as exc:
                raise _RefusedError(403, exc.code) from None
            for raw in addresses:
                address = ipaddress.ip_address(raw)
                # The proxy's own services (on any of its interfaces) and the platform's
                # internal networks are off limits whatever an environment allows.
                if (
                    address.is_loopback
                    or any(address in network for network in self._deny)
                    or self._own_address(raw)
                ):
                    raise _RefusedError(403, "platform_address")
        except _RefusedError as exc:
            rules.blocked_count += 1
            rules.blocked.append({"host": origin.host, "port": origin.port, "reason": exc.reason})
            logger.info(
                "egress_blocked",
                extra={"host": origin.host, "port": origin.port, "reason": exc.reason},
            )
            raise
        rules.allowed_count += 1
        return addresses[0]

    async def _relay(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        up_reader: asyncio.StreamReader,
        up_writer: asyncio.StreamWriter,
    ) -> None:
        async def pump(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
            try:
                while True:
                    chunk = await asyncio.wait_for(source.read(65536), self._idle_timeout_s)
                    if not chunk:
                        break
                    sink.write(chunk)
                    await sink.drain()
            except (TimeoutError, ConnectionError, OSError):
                pass
            finally:
                with contextlib.suppress(ConnectionError, OSError):
                    if sink.can_write_eof():
                        sink.write_eof()

        tasks = [
            asyncio.create_task(pump(client_reader, up_writer)),
            asyncio.create_task(pump(up_reader, client_writer)),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()


def _parse_head(head: bytes) -> tuple[str, str, list[tuple[str, str]]]:
    try:
        lines = head.decode("latin-1").split("\r\n")
        method, target, version = lines[0].split(" ")
    except ValueError:
        raise _RefusedError(400, "bad_request") from None
    if not version.startswith("HTTP/1."):
        raise _RefusedError(400, "bad_request")
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        name, separator, value = line.partition(":")
        if not separator or not name or name != name.strip():
            raise _RefusedError(400, "bad_request")
        headers.append((name, value.strip()))
    return method.upper(), target, headers


def _destination(method: str, target: str, headers: list[tuple[str, str]]) -> tuple[Origin, bytes]:
    """Where the request wants to go, and (for plain HTTP) the request to pass on."""
    if method == "CONNECT":
        host, separator, port = target.rpartition(":")
        if not separator or not port.isdigit() or not 0 < int(port) < 65536:
            raise _RefusedError(400, "bad_request")
        try:
            return Origin("https", normalize_host(host.strip("[]")), int(port)), b""
        except (UrlNotAllowedError, ValueError):
            raise _RefusedError(400, "bad_request") from None
    if not target.lower().startswith("http://"):
        raise _RefusedError(400, "absolute_http_url_required")
    try:
        origin = parse_origin(target, allow_http=True)
    except UrlNotAllowedError as exc:
        raise _RefusedError(403, exc.code) from None
    rest = target[len("http://") :]
    path = rest[rest.index("/") :] if "/" in rest else "/"
    if "?" in rest and "/" not in rest.split("?", 1)[0]:
        path = "/?" + rest.split("?", 1)[1]
    upgrade = any(k.lower() == "upgrade" for k, _ in headers)
    kept = [
        (k, v)
        for k, v in headers
        if k.lower() not in _HOP_HEADERS and (upgrade or k.lower() != "connection")
    ]
    if not upgrade:
        # One request per connection: every request is checked, none rides along.
        kept.append(("Connection", "close"))
    lines = [f"{method} {path} HTTP/1.1", *(f"{k}: {v}" for k, v in kept), "", ""]
    return origin, "\r\n".join(lines).encode("latin-1")


async def _respond(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
    phrases = {
        400: "Bad Request",
        403: "Forbidden",
        407: "Proxy Authentication Required",
        502: "Bad Gateway",
    }
    body = f"Blocked by the test platform: {reason}\n".encode()
    lines = [
        f"HTTP/1.1 {status} {phrases.get(status, 'Error')}",
        "Content-Type: text/plain; charset=utf-8",
        f"Content-Length: {len(body)}",
        f"X-Egress-Reason: {reason}",
        "Connection: close",
    ]
    if status == 407:
        lines.append('Proxy-Authenticate: Basic realm="egress"')
    with contextlib.suppress(ConnectionError, OSError):
        writer.write("\r\n".join([*lines, "", ""]).encode() + body)
        await writer.drain()
