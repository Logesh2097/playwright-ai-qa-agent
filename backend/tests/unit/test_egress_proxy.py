"""The egress proxy, over real loopback sockets with a fake resolver and upstream."""

import asyncio
import base64
import ipaddress
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import pytest
from pydantic import SecretStr

from app.core.security.url_safety import AccessValidator, parse_origin
from app.egress.config import EgressSettings
from app.egress.main import create_app
from app.egress.proxy import EgressProxy, SessionRegistry, is_own_address

SESSION = "0b0e6a2e-58a5-4a52-9d5e-0c1f8a7f3c11"
SECRET = "proxy-secret-0123456789"
PUBLIC = "93.184.216.34"
OWN = "172.20.0.7"  # stands for the proxy's address on its outward-facing network


@dataclass
class Net:
    """Fake DNS plus a recording upstream that every permitted connection lands on."""

    dns: dict[str, list[str]] = field(default_factory=dict)
    connected_to: list[tuple[str, int]] = field(default_factory=list)
    received: list[bytes] = field(default_factory=list)
    upstream_port: int = 0

    async def resolve(self, host: str, port: int) -> list[str]:
        return self.dns.get(host, [])

    async def connect(
        self, address: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        self.connected_to.append((address, port))
        return await asyncio.open_connection("127.0.0.1", self.upstream_port)

    async def upstream(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(65536)
        self.received.append(data)
        if data.startswith(b"ping"):
            writer.write(b"pong")
        else:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()


@dataclass
class Harness:
    net: Net
    registry: SessionRegistry
    port: int
    now: list[float]

    def allow(
        self, *origins: str, private: bool = False, allow_http: bool = True, ttl_s: float = 60
    ) -> None:
        self.registry.register(
            SESSION,
            secret=SECRET,
            allowed_origins=[parse_origin(o) for o in origins],
            allow_private_network=private,
            allow_http=allow_http,
            ttl_s=ttl_s,
        )

    async def send(self, request: str, *, auth: str | None = SECRET, then: bytes = b"") -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        head = request
        if auth is not None:
            token = base64.b64encode(f"{SESSION}:{auth}".encode()).decode()
            head += f"Proxy-Authorization: Basic {token}\r\n"
        writer.write((head + "\r\n").encode())
        await writer.drain()
        if then:
            first = await reader.readuntil(b"\r\n\r\n")
            writer.write(then)
            await writer.drain()
            rest = await asyncio.wait_for(reader.read(65536), 3)
            writer.close()
            return first + rest
        data = await asyncio.wait_for(reader.read(65536), 3)
        writer.close()
        return data

    def blocked(self) -> list[str]:
        rules = self.registry.authenticate(SESSION, SECRET)
        assert rules is not None
        return [entry["reason"] for entry in rules.blocked]


@pytest.fixture
async def proxy() -> AsyncIterator[Harness]:
    net = Net(dns={"shop.example": [PUBLIC], "intranet.example": ["10.0.0.5"]})
    now = [1000.0]
    registry = SessionRegistry(clock=lambda: now[0])
    upstream = await asyncio.start_server(net.upstream, "127.0.0.1", 0)
    net.upstream_port = upstream.sockets[0].getsockname()[1]
    egress = EgressProxy(
        registry,
        AccessValidator(net),
        deny_networks=[ipaddress.ip_network("10.213.77.0/24")],
        connector=net.connect,
        own_address=lambda address: address == OWN,
    )
    server = await egress.serve("127.0.0.1", 0)
    try:
        yield Harness(net, registry, server.sockets[0].getsockname()[1], now)
    finally:
        server.close()
        upstream.close()
        await server.wait_closed()
        await upstream.wait_closed()


GET = "GET http://shop.example/cart?item=2 HTTP/1.1\r\nHost: shop.example\r\nAccept: */*\r\n"


def _status(response: bytes) -> int:
    return int(response.split(b" ", 2)[1])


def _reason(response: bytes) -> str:
    for line in response.split(b"\r\n"):
        if line.lower().startswith(b"x-egress-reason:"):
            return line.split(b":", 1)[1].strip().decode()
    return ""


def _refused(response: bytes, reason: str) -> bool:
    """A refused tunnel gets a 403. A refused plain request gets no answer at all, so
    that the browser reports a failed load rather than showing an error page as the page."""
    if response == b"":
        return True
    return (_status(response), _reason(response)) == (403, reason)


# --- who is asking ------------------------------------------------------------------------


async def test_requests_without_valid_session_credentials_are_refused(proxy: Harness) -> None:
    proxy.allow("http://shop.example")

    anonymous = await proxy.send(GET, auth=None)
    wrong = await proxy.send(GET, auth="not-the-secret")

    assert _status(anonymous) == _status(wrong) == 407
    assert b'Proxy-Authenticate: Basic realm="egress"' in anonymous
    assert proxy.net.connected_to == []


async def test_rules_stop_working_when_the_session_expires(proxy: Harness) -> None:
    proxy.allow("http://shop.example", ttl_s=30)
    assert _status(await proxy.send(GET)) == 200

    proxy.now[0] += 31

    assert _status(await proxy.send(GET)) == 407
    assert len(proxy.registry) == 0


# --- allowed traffic ----------------------------------------------------------------------


async def test_allowed_http_request_is_forwarded_to_the_address_that_was_checked(
    proxy: Harness,
) -> None:
    proxy.allow("http://shop.example")

    response = await proxy.send(GET)

    assert _status(response) == 200 and response.endswith(b"ok")
    # Connected to the resolved address, not to the name (no second lookup to subvert).
    assert proxy.net.connected_to == [(PUBLIC, 80)]
    forwarded = proxy.net.received[0].decode()
    assert forwarded.startswith("GET /cart?item=2 HTTP/1.1\r\n")
    assert "Host: shop.example" in forwarded and "Connection: close" in forwarded
    # The session's proxy credentials are not passed on to the application.
    assert "Proxy-Authorization" not in forwarded and SECRET not in forwarded


async def test_allowed_https_origin_gets_a_tunnel(proxy: Harness) -> None:
    proxy.allow("https://shop.example")

    response = await proxy.send(
        "CONNECT shop.example:443 HTTP/1.1\r\nHost: shop.example:443\r\n", then=b"ping"
    )

    assert response.startswith(b"HTTP/1.1 200 Connection established")
    assert response.endswith(b"pong")
    assert proxy.net.connected_to == [(PUBLIC, 443)]
    assert proxy.net.received == [b"ping"]


# --- refused traffic ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("request_line", "reason"),
    [
        ("GET http://evil.example/ HTTP/1.1\r\nHost: evil.example\r\n", "origin_not_allowlisted"),
        (
            "GET http://shop.example:8080/ HTTP/1.1\r\nHost: shop.example:8080\r\n",
            "origin_not_allowlisted",
        ),
        ("CONNECT shop.example:443 HTTP/1.1\r\n", "origin_not_allowlisted"),  # only http is allowed
        ("CONNECT evil.example:443 HTTP/1.1\r\n", "origin_not_allowlisted"),
        ("GET http://169.254.169.254/latest/meta-data/ HTTP/1.1\r\n", "origin_not_allowlisted"),
    ],
)
async def test_anything_outside_the_allowed_origins_is_refused_and_noted(
    proxy: Harness, request_line: str, reason: str
) -> None:
    proxy.allow("http://shop.example")

    response = await proxy.send(request_line)

    assert _refused(response, reason)
    assert (response == b"") is request_line.startswith("GET")
    assert proxy.net.connected_to == []
    assert proxy.blocked() == [reason]


async def test_private_addresses_need_the_environment_to_allow_them(proxy: Harness) -> None:
    request = "GET http://intranet.example/ HTTP/1.1\r\nHost: intranet.example\r\n"
    proxy.allow("http://intranet.example")
    assert await proxy.send(request) == b""
    assert proxy.blocked() == ["address_not_allowed"]

    proxy.allow("http://intranet.example", private=True)
    assert _status(await proxy.send(request)) == 200
    assert proxy.net.connected_to == [("10.0.0.5", 80)]


async def test_a_name_with_one_forbidden_address_is_refused_whole(proxy: Harness) -> None:
    """DNS rebinding: a public and a private record for the same name."""
    proxy.net.dns["shop.example"] = [PUBLIC, "127.0.0.1"]
    proxy.allow("http://shop.example")

    assert await proxy.send(GET) == b""
    assert proxy.blocked() == ["address_not_allowed"]
    assert proxy.net.connected_to == []


@pytest.mark.parametrize("address", ["10.213.77.3", "127.0.0.1", "169.254.169.254", OWN])
async def test_platform_loopback_and_metadata_addresses_are_refused_even_with_private_allowed(
    proxy: Harness, address: str
) -> None:
    proxy.net.dns["sneaky.example"] = [address]
    proxy.allow("http://sneaky.example", "https://sneaky.example", private=True)

    plain = await proxy.send("GET http://sneaky.example/ HTTP/1.1\r\nHost: sneaky.example\r\n")
    tunnel = await proxy.send("CONNECT sneaky.example:443 HTTP/1.1\r\n")

    assert plain == b"" and _status(tunnel) == 403
    assert len(proxy.blocked()) == 2
    assert proxy.net.connected_to == []


async def test_plain_http_is_refused_where_only_https_is_allowed(proxy: Harness) -> None:
    proxy.allow("http://shop.example", allow_http=False)
    assert await proxy.send(GET) == b""
    assert proxy.blocked() == ["origin_not_allowlisted"]


async def test_unresolvable_and_malformed_requests_are_refused(proxy: Harness) -> None:
    proxy.allow("http://ghost.example")
    assert await proxy.send("GET http://ghost.example/ HTTP/1.1\r\nHost: ghost.example\r\n") == b""
    assert proxy.blocked() == ["host_not_resolvable"]
    assert _status(await proxy.send("GET /relative HTTP/1.1\r\nHost: shop.example\r\n")) == 400
    assert _status(await proxy.send("CONNECT shop.example HTTP/1.1\r\n")) == 400
    assert _status(await proxy.send("NONSENSE\r\n")) == 400


def test_own_addresses_are_recognised_by_trying_to_bind_them() -> None:
    assert is_own_address("127.0.0.1")
    assert not is_own_address(PUBLIC)
    assert not is_own_address("2001:db8::1")


# --- admin API ----------------------------------------------------------------------------

ADMIN = "egress-admin-token-0123456789abcdef"


async def test_admin_api_needs_its_token_and_reports_what_was_blocked() -> None:
    app = create_app(EgressSettings(egress_admin_token=SecretStr(ADMIN), egress_proxy_port=3128))
    rules = {"secret": SECRET, "allowed_origins": ["https://shop.example"], "ttl_s": 60}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://egress"
    ) as client:
        assert (await client.get("/livez")).status_code == 200
        assert (await client.put(f"/sessions/{SESSION}", json=rules)).status_code == 401
        assert (await client.get("/healthz")).status_code == 401

        client.headers["Authorization"] = f"Bearer {ADMIN}"
        assert (await client.put(f"/sessions/{SESSION}", json=rules)).status_code == 204
        assert (await client.get("/healthz")).json() == {"status": "ok", "sessions": 1}
        bad = {**rules, "allowed_origins": ["ftp://shop.example"]}
        assert (await client.put(f"/sessions/{SESSION}", json=bad)).status_code == 422

        registry: SessionRegistry = app.state.registry
        found = registry.authenticate(SESSION, SECRET)
        assert found is not None and parse_origin("https://shop.example") in found.allowed_origins
        found.blocked.append(
            {"host": "evil.example", "port": 443, "reason": "origin_not_allowlisted"}
        )
        found.blocked_count += 1

        removed = await client.delete(f"/sessions/{SESSION}")
        assert removed.json()["blocked"][0]["host"] == "evil.example"
        assert (await client.delete(f"/sessions/{SESSION}")).status_code == 404
        assert registry.authenticate(SESSION, SECRET) is None
