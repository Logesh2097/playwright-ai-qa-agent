"""Decides which network destinations the platform may contact on a user's behalf.

Users give the platform URLs and the platform then makes requests to them, which
is the classic setting for server-side request forgery. Every such destination
goes through the same three checks, in this order:

1. **Syntax** - http(s) only, a real hostname, no embedded credentials.
2. **Allowlist** - the origin (scheme, host, port) is one the environment lists.
3. **Address** - the host is resolved and *every* resulting IP address must be
   public, unless an administrator enabled private networks for the environment.
   Link-local and cloud-metadata addresses are refused regardless.

A caller that goes on to open a connection must connect to one of the addresses
returned here, not resolve the name a second time. Otherwise the name could
resolve to something else between check and use (DNS rebinding).

`AccessValidator.validate` is the single entry point and must be called again
for every redirect and every navigation, not only for the first URL.
"""

import asyncio
import ipaddress
import re
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import SplitResult, urlsplit

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_DEFAULT_PORTS = {"http": 80, "https": 443}
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_MAX_URL_LENGTH = 2048

# Addresses that are never legitimate test targets, even on a private network.
_ALWAYS_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "169.254.0.0/16",  # link-local, incl. the 169.254.169.254 metadata service
        "fe80::/10",  # IPv6 link-local
        "fd00:ec2::/32",  # AWS IPv6 metadata service
        "100.100.100.200/32",  # Alibaba Cloud metadata
        "192.0.0.192/32",  # Oracle Cloud metadata
        "0.0.0.0/8",
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved, incl. broadcast
        "ff00::/8",  # IPv6 multicast
    )
)
_ALWAYS_BLOCKED_HOSTS = frozenset({"metadata.google.internal", "metadata", "instance-data"})


class UrlNotAllowedError(Exception):
    """A destination failed validation. `code` is stable and safe to show to users."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True, order=True)
class Origin:
    scheme: str
    host: str
    port: int

    @property
    def netloc(self) -> str:
        """Host, plus the port when it is not the scheme's default. As used in a Host header."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        if _DEFAULT_PORTS.get(self.scheme) == self.port:
            return host
        return f"{host}:{self.port}"

    def __str__(self) -> str:
        return f"{self.scheme}://{self.netloc}"


def normalize_host(host: str) -> str:
    """Lower-case, strip the root dot, convert international names to ASCII (punycode)."""
    host = host.strip().rstrip(".").lower()
    if not host:
        raise UrlNotAllowedError("invalid_host", "The URL has no host.")
    literal = _ip_literal(host)
    if literal is not None:
        return str(literal)
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UrlNotAllowedError("invalid_host", "The host name is not valid.") from exc
    labels = ascii_host.split(".")
    if len(ascii_host) > 253 or not all(_LABEL.fullmatch(label) for label in labels):
        raise UrlNotAllowedError("invalid_host", "The host name is not valid.")
    # An all-numeric last label ("2130706433", "0x7f.1", "127.1") is an IP address in
    # disguise for some resolvers. Real top-level domains are never numeric.
    if labels[-1].isdigit() or labels[-1].startswith("0x"):
        raise UrlNotAllowedError("invalid_host", "The host name is not valid.")
    return ascii_host


def _ip_literal(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def parse_origin(url: str, *, allow_http: bool = True) -> Origin:
    """Validate the syntax of a URL and return its origin."""
    return _parse(url, allow_http=allow_http)[0]


def _parse(url: str, *, allow_http: bool) -> tuple[Origin, SplitResult]:
    if not isinstance(url, str) or not url or len(url) > _MAX_URL_LENGTH:
        raise UrlNotAllowedError("invalid_url", "The URL is missing or too long.")
    if url != url.strip() or any(ord(character) < 0x21 for character in url):
        raise UrlNotAllowedError(
            "invalid_url", "The URL contains whitespace or control characters."
        )
    if "\\" in url:
        raise UrlNotAllowedError("invalid_url", "The URL contains a backslash.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UrlNotAllowedError("invalid_url", "The URL is not valid.") from exc

    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise UrlNotAllowedError("invalid_scheme", "Only http and https URLs are allowed.")
    if scheme == "http" and not allow_http:
        raise UrlNotAllowedError("https_required", "This environment requires an https URL.")
    if parts.username is not None or parts.password is not None:
        raise UrlNotAllowedError(
            "credentials_in_url", "The URL must not contain a username or password."
        )
    if not parts.hostname:
        raise UrlNotAllowedError("invalid_host", "The URL has no host.")
    host = normalize_host(parts.hostname)
    return Origin(scheme=scheme, host=host, port=port or _DEFAULT_PORTS[scheme]), parts


def normalize_domain(domain: str) -> str:
    """Validate one entry of an application's allowed-domain list."""
    domain = domain.strip()
    if any(character in domain for character in "*/@:? \t") and _ip_literal(domain) is None:
        raise UrlNotAllowedError(
            "invalid_domain",
            "Enter a plain host name such as staging.example.com, without wildcards.",
        )
    host = normalize_host(domain)
    if _ip_literal(host) is None and "." not in host and host != "localhost":
        raise UrlNotAllowedError(
            "invalid_domain", "A domain needs at least two parts, such as example.com."
        )
    return host


def host_in_domains(host: str, domains: Iterable[str]) -> bool:
    """True if `host` equals one of the domains or is a subdomain of one."""
    is_ip = _ip_literal(host) is not None
    for domain in domains:
        if host == domain:
            return True
        # Subdomain matching only makes sense for names: "1.2.3.4" is not under "3.4".
        if not is_ip and _ip_literal(domain) is None and host.endswith(f".{domain}"):
            return True
    return False


def address_block_reason(address: IPAddress, *, allow_private: bool) -> str | None:
    """Why an IP address may not be contacted, or None if it may."""
    if isinstance(address, ipaddress.IPv6Address):
        # Addresses that embed an IPv4 address are judged by that IPv4 address,
        # otherwise ::ffff:127.0.0.1 or a NAT64 form would slip through.
        embedded = address.ipv4_mapped or address.sixtofour
        if embedded is None and address.teredo is not None:
            embedded = address.teredo[1]
        if embedded is None and address in ipaddress.ip_network("64:ff9b::/96"):
            embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        if embedded is not None:
            return address_block_reason(embedded, allow_private=allow_private)

    if any(address in network for network in _ALWAYS_BLOCKED_NETWORKS):
        return "link-local, metadata or reserved address"
    if address.is_unspecified or address.is_multicast:
        return "reserved address"
    if address.is_global:
        return None
    # Loopback is tested before anything else non-public: Python classes ::1 as
    # "reserved" as well, which must not hide that it is plain loopback.
    if address.is_loopback:
        return None if allow_private else "loopback address"
    if address.is_private:
        return None if allow_private else "private network address"
    return "non-public address"


class Resolver(Protocol):
    async def resolve(self, host: str, port: int) -> list[str]:
        """Return every address the host resolves to; an empty list if it does not resolve."""
        ...


class SystemResolver:
    async def resolve(self, host: str, port: int) -> list[str]:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError):
            return []
        return list(dict.fromkeys(str(info[4][0]) for info in infos))


@dataclass(frozen=True, slots=True)
class DestinationPolicy:
    """What one environment is allowed to reach."""

    allowed_origins: frozenset[Origin]
    allow_private_network: bool = False
    allow_http: bool = True


@dataclass(frozen=True, slots=True)
class ValidatedDestination:
    url: str
    origin: Origin
    # Connect to one of these; do not resolve the host again.
    addresses: tuple[str, ...]
    path_and_query: str


class AccessValidator:
    def __init__(self, resolver: Resolver | None = None, *, dns_timeout_s: float = 3.0) -> None:
        self._resolver = resolver or SystemResolver()
        self._dns_timeout_s = dns_timeout_s

    async def resolve_public(self, origin: Origin, *, allow_private: bool) -> tuple[str, ...]:
        """Resolve a host and insist that every address it maps to is acceptable."""
        if origin.host in _ALWAYS_BLOCKED_HOSTS:
            raise UrlNotAllowedError("address_not_allowed", "This host is not allowed.")
        literal = _ip_literal(origin.host)
        if literal is not None:
            addresses = [str(literal)]
        else:
            try:
                addresses = await asyncio.wait_for(
                    self._resolver.resolve(origin.host, origin.port), timeout=self._dns_timeout_s
                )
            except TimeoutError:
                addresses = []
        if not addresses:
            raise UrlNotAllowedError(
                "host_not_resolvable", f"The host {origin.host} could not be resolved."
            )
        for raw in addresses:
            reason = address_block_reason(ipaddress.ip_address(raw), allow_private=allow_private)
            if reason is not None:
                # One bad address is enough: a name with a public and a private record
                # could be steered to the private one.
                raise UrlNotAllowedError(
                    "address_not_allowed",
                    f"The host {origin.host} resolves to a {reason}, which is not allowed.",
                )
        return tuple(addresses)

    async def validate(self, url: str, policy: DestinationPolicy) -> ValidatedDestination:
        """Full check of one destination. Call for every hop: initial URL, redirect, navigation."""
        origin, parts = _parse(url, allow_http=policy.allow_http)
        if origin not in policy.allowed_origins:
            raise UrlNotAllowedError(
                "origin_not_allowlisted",
                f"{origin} is not in this environment's list of allowed origins.",
            )
        addresses = await self.resolve_public(origin, allow_private=policy.allow_private_network)
        path = parts.path or "/"
        return ValidatedDestination(
            url=url,
            origin=origin,
            addresses=addresses,
            path_and_query=f"{path}?{parts.query}" if parts.query else path,
        )
