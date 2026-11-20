"""SSRF defences: URL syntax, address rules, DNS validation, redirects and pinning."""

import ipaddress

import httpx
import pytest

from app.core.security.url_safety import (
    AccessValidator,
    DestinationPolicy,
    Origin,
    UrlNotAllowedError,
    address_block_reason,
    host_in_domains,
    normalize_domain,
    parse_origin,
)
from app.services.access_validation import AccessProbe

PUBLIC = "93.184.216.34"


class Dns:
    def __init__(self, records: dict[str, list[str]]) -> None:
        self.records = records
        self.lookups: list[str] = []

    async def resolve(self, host: str, port: int) -> list[str]:
        self.lookups.append(host)
        return list(self.records.get(host, []))


def policy(*origins: str, private: bool = False, http: bool = True) -> DestinationPolicy:
    return DestinationPolicy(
        allowed_origins=frozenset(parse_origin(o) for o in origins),
        allow_private_network=private,
        allow_http=http,
    )


# --- syntax ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://staging.acme.io", Origin("https", "staging.acme.io", 443)),
        ("https://Staging.ACME.io./login?next=/a", Origin("https", "staging.acme.io", 443)),
        ("http://acme.io:8080/x", Origin("http", "acme.io", 8080)),
        ("https://bücher.example", Origin("https", "xn--bcher-kva.example", 443)),
        ("http://127.0.0.1:3000", Origin("http", "127.0.0.1", 3000)),
        ("http://[::1]:3000/", Origin("http", "::1", 3000)),
    ],
)
def test_valid_urls_are_normalised(url: str, expected: Origin) -> None:
    assert parse_origin(url) == expected


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("ftp://acme.io", "invalid_scheme"),
        ("file:///etc/passwd", "invalid_scheme"),
        ("javascript:alert(1)", "invalid_scheme"),
        ("gopher://acme.io", "invalid_scheme"),
        ("//acme.io/path", "invalid_scheme"),
        ("acme.io", "invalid_scheme"),
        ("https://user:pass@acme.io", "credentials_in_url"),
        ("https://acme.io@evil.example", "credentials_in_url"),
        ("https://", "invalid_host"),
        ("https:///path", "invalid_host"),
        ("https://acme .io", "invalid_url"),
        ("https://acme.io\\@evil.example", "invalid_url"),
        ("https://acme.io\n.evil.example", "invalid_url"),
        (" https://acme.io", "invalid_url"),
        ("https://acme.io:99999", "invalid_url"),
        ("https://-bad.acme.io", "invalid_host"),
        ("https://a_b.acme.io", "invalid_host"),
        # IP addresses in disguise
        ("http://2130706433/", "invalid_host"),
        ("http://127.1/", "invalid_host"),
        ("http://0x7f.0.0.1/", "invalid_host"),
        ("http://017700000001/", "invalid_host"),
        ("https://" + "a" * 2100 + ".io", "invalid_url"),
        ("", "invalid_url"),
    ],
)
def test_malformed_and_deceptive_urls_are_rejected(url: str, code: str) -> None:
    with pytest.raises(UrlNotAllowedError) as excinfo:
        parse_origin(url)
    assert excinfo.value.code == code


def test_https_can_be_required() -> None:
    with pytest.raises(UrlNotAllowedError) as excinfo:
        parse_origin("http://acme.io", allow_http=False)
    assert excinfo.value.code == "https_required"


def test_origin_text_omits_default_ports() -> None:
    assert str(Origin("https", "acme.io", 443)) == "https://acme.io"
    assert str(Origin("https", "acme.io", 8443)) == "https://acme.io:8443"
    assert str(Origin("http", "::1", 3000)) == "http://[::1]:3000"


# --- domain lists ------------------------------------------------------------------


def test_domain_covers_itself_and_its_subdomains_only() -> None:
    domains = ["acme.io", "10.0.0.5"]
    assert host_in_domains("acme.io", domains)
    assert host_in_domains("staging.acme.io", domains)
    assert host_in_domains("a.b.acme.io", domains)
    assert host_in_domains("10.0.0.5", domains)
    # look-alikes
    assert not host_in_domains("evilacme.io", domains)
    assert not host_in_domains("acme.io.evil.example", domains)
    assert not host_in_domains("acme.com", domains)
    assert not host_in_domains("1.10.0.0.5", domains)


@pytest.mark.parametrize(
    "entry", ["*.acme.io", "acme.io/path", "https://acme.io", "com", "a b.io", ""]
)
def test_invalid_domain_entries_are_rejected(entry: str) -> None:
    with pytest.raises(UrlNotAllowedError):
        normalize_domain(entry)


def test_domain_entries_are_normalised() -> None:
    assert normalize_domain(" Staging.ACME.io. ") == "staging.acme.io"
    assert normalize_domain("localhost") == "localhost"


# --- address rules -----------------------------------------------------------------

_PRIVATE = ["10.0.0.5", "172.16.3.4", "192.168.1.1", "127.0.0.1", "::1", "fd12:3456::1"]
_NEVER = [
    "169.254.169.254",  # AWS / GCP / Azure metadata
    "169.254.0.1",
    "fe80::1",
    "fd00:ec2::254",  # AWS IPv6 metadata
    "100.100.100.200",  # Alibaba metadata
    "0.0.0.0",  # noqa: S104 - a test value
    "224.0.0.1",
    "255.255.255.255",
    "::",
    "::ffff:169.254.169.254",  # IPv4-mapped metadata
]
_DISGUISED_PRIVATE = [
    "::ffff:127.0.0.1",  # IPv4-mapped
    "::ffff:10.0.0.5",
    "2002:7f00:1::",  # 6to4 wrapping 127.0.0.1
    "64:ff9b::a00:5",  # NAT64 wrapping 10.0.0.5
]


@pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert address_block_reason(ipaddress.ip_address(address), allow_private=False) is None


@pytest.mark.parametrize("address", _PRIVATE + _DISGUISED_PRIVATE + ["100.64.0.1"])
def test_private_and_loopback_addresses_are_blocked_by_default(address: str) -> None:
    assert address_block_reason(ipaddress.ip_address(address), allow_private=False) is not None


@pytest.mark.parametrize("address", _PRIVATE + _DISGUISED_PRIVATE)
def test_private_addresses_are_allowed_only_when_explicitly_permitted(address: str) -> None:
    assert address_block_reason(ipaddress.ip_address(address), allow_private=True) is None


@pytest.mark.parametrize("address", _NEVER)
def test_metadata_and_link_local_are_blocked_even_on_private_networks(address: str) -> None:
    assert address_block_reason(ipaddress.ip_address(address), allow_private=True) is not None
    assert address_block_reason(ipaddress.ip_address(address), allow_private=False) is not None


# --- validator: allowlist + DNS ------------------------------------------------------


async def test_validate_returns_the_addresses_to_connect_to() -> None:
    validator = AccessValidator(Dns({"staging.acme.io": [PUBLIC]}))
    destination = await validator.validate(
        "https://staging.acme.io/login?x=1", policy("https://staging.acme.io")
    )
    assert destination.addresses == (PUBLIC,)
    assert destination.path_and_query == "/login?x=1"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/",  # other host
        "http://staging.acme.io/",  # other scheme
        "https://staging.acme.io:8443/",  # other port
        "https://www.staging.acme.io/",  # subdomain is a different origin
        "https://staging.acme.io.evil.example/",
    ],
)
async def test_origins_outside_the_allowlist_are_rejected_before_any_lookup(url: str) -> None:
    dns = Dns({})
    with pytest.raises(UrlNotAllowedError) as excinfo:
        await AccessValidator(dns).validate(url, policy("https://staging.acme.io"))
    assert excinfo.value.code == "origin_not_allowlisted"
    assert dns.lookups == []


@pytest.mark.parametrize(
    ("records", "code"),
    [
        (["10.0.0.5"], "address_not_allowed"),
        (["127.0.0.1"], "address_not_allowed"),
        (["169.254.169.254"], "address_not_allowed"),
        # One private record among public ones is enough to refuse.
        ([PUBLIC, "192.168.0.10"], "address_not_allowed"),
        (["::ffff:10.0.0.1"], "address_not_allowed"),
        ([], "host_not_resolvable"),
    ],
)
async def test_allowlisted_name_resolving_to_a_forbidden_address_is_rejected(
    records: list[str], code: str
) -> None:
    validator = AccessValidator(Dns({"staging.acme.io": records}))
    with pytest.raises(UrlNotAllowedError) as excinfo:
        await validator.validate("https://staging.acme.io/", policy("https://staging.acme.io"))
    assert excinfo.value.code == code


async def test_private_targets_need_the_explicit_permission() -> None:
    validator = AccessValidator(Dns({"localhost": ["127.0.0.1"]}))
    url = "http://localhost:3000/"
    with pytest.raises(UrlNotAllowedError):
        await validator.validate(url, policy("http://localhost:3000"))
    allowed = await validator.validate(url, policy("http://localhost:3000", private=True))
    assert allowed.addresses == ("127.0.0.1",)

    # The permission still does not open the metadata service.
    with pytest.raises(UrlNotAllowedError):
        await validator.validate(
            "http://169.254.169.254/latest/meta-data",
            policy("http://169.254.169.254", private=True),
        )


async def test_ip_literals_are_checked_without_dns() -> None:
    dns = Dns({})
    validator = AccessValidator(dns)
    with pytest.raises(UrlNotAllowedError) as excinfo:
        await validator.validate("http://10.0.0.5/", policy("http://10.0.0.5"))
    assert excinfo.value.code == "address_not_allowed"
    assert dns.lookups == []


# --- probe: redirects, pinning, rebinding ---------------------------------------------


class Web:
    def __init__(self, routes: dict[str, tuple[int, dict[str, str]]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.url.scheme}://{request.headers['host']}{request.url.path}"
        status, headers = self.routes.get(key, (200, {}))
        return httpx.Response(status, headers=headers, text="internal page")


def make_probe(dns: Dns, web: Web, **kwargs: int) -> AccessProbe:
    return AccessProbe(AccessValidator(dns), transport=httpx.MockTransport(web), **kwargs)


async def test_probe_connects_to_the_validated_address_with_the_real_host_name() -> None:
    dns = Dns({"staging.acme.io": [PUBLIC]})
    web = Web({})
    result = await make_probe(dns, web).probe(
        "https://staging.acme.io/health", policy("https://staging.acme.io")
    )
    assert result.reachable and result.status == 200
    request = web.requests[0]
    assert request.url.host == PUBLIC, "the connection goes to the checked IP"
    assert request.headers["host"] == "staging.acme.io"
    assert request.extensions["sni_hostname"] == "staging.acme.io"
    assert dns.lookups == ["staging.acme.io"], "resolved once; never re-resolved for the request"


async def test_probe_follows_redirects_inside_the_allowlist() -> None:
    dns = Dns({"staging.acme.io": [PUBLIC], "auth.acme.io": [PUBLIC]})
    web = Web(
        {
            "https://staging.acme.io/": (302, {"location": "/login"}),
            "https://staging.acme.io/login": (302, {"location": "https://auth.acme.io/sso"}),
        }
    )
    result = await make_probe(dns, web).probe(
        "https://staging.acme.io/", policy("https://staging.acme.io", "https://auth.acme.io")
    )
    assert result.reachable
    assert result.final_url == "https://auth.acme.io/sso"
    assert [hop.status for hop in result.hops] == [302, 302, 200]


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/collect",
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost:8000/admin",
        "http://staging.acme.io/",  # downgrade to http is a different origin
        "//evil.example/x",
        "file:///etc/passwd",
    ],
)
async def test_redirect_that_leaves_the_allowlist_is_not_followed(location: str) -> None:
    dns = Dns(
        {"staging.acme.io": [PUBLIC], "evil.example": ["203.0.113.9"], "localhost": ["127.0.0.1"]}
    )
    web = Web({"https://staging.acme.io/": (302, {"location": location})})
    result = await make_probe(dns, web).probe(
        "https://staging.acme.io/", policy("https://staging.acme.io")
    )
    assert not result.reachable
    assert result.error_code in {"origin_not_allowlisted", "invalid_scheme"}
    assert len(web.requests) == 1, "the redirect target was never contacted"
    assert result.hops[-1].status is None and result.hops[-1].blocked_code is not None


async def test_redirect_to_an_allowlisted_name_is_revalidated_against_dns() -> None:
    """The redirect target is allowlisted, but now resolves to an internal address."""
    dns = Dns({"staging.acme.io": [PUBLIC], "auth.acme.io": ["10.1.2.3"]})
    web = Web({"https://staging.acme.io/": (302, {"location": "https://auth.acme.io/"})})
    result = await make_probe(dns, web).probe(
        "https://staging.acme.io/", policy("https://staging.acme.io", "https://auth.acme.io")
    )
    assert not result.reachable
    assert result.error_code == "address_not_allowed"
    assert len(web.requests) == 1


async def test_dns_rebinding_between_hops_is_caught() -> None:
    """First lookup is public, the next lookup of the same name is loopback."""

    class Rebinding(Dns):
        async def resolve(self, host: str, port: int) -> list[str]:
            self.lookups.append(host)
            return [PUBLIC] if len(self.lookups) == 1 else ["127.0.0.1"]

    dns = Rebinding({})
    web = Web({"https://staging.acme.io/": (302, {"location": "/next"})})
    result = await make_probe(dns, web).probe(
        "https://staging.acme.io/", policy("https://staging.acme.io")
    )
    assert not result.reachable
    assert result.error_code == "address_not_allowed"
    assert [request.url.host for request in web.requests] == [PUBLIC]


async def test_redirect_loops_are_bounded() -> None:
    dns = Dns({"staging.acme.io": [PUBLIC]})
    web = Web({"https://staging.acme.io/": (302, {"location": "/"})})
    result = await make_probe(dns, web, max_redirects=3).probe(
        "https://staging.acme.io/", policy("https://staging.acme.io")
    )
    assert result.error_code == "too_many_redirects"
    assert len(web.requests) == 4


async def test_probe_reports_server_errors_and_connection_failures() -> None:
    dns = Dns({"staging.acme.io": [PUBLIC]})
    down = await make_probe(dns, Web({"https://staging.acme.io/": (503, {})})).probe(
        "https://staging.acme.io/", policy("https://staging.acme.io")
    )
    assert (down.reachable, down.status, down.error_code) == (False, 503, "server_error")

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    probe = AccessProbe(AccessValidator(dns), transport=httpx.MockTransport(refuse))
    failed = await probe.probe("https://staging.acme.io/", policy("https://staging.acme.io"))
    assert (failed.reachable, failed.error_code) == (False, "connection_failed")
    assert "refused" not in (failed.error or "")
