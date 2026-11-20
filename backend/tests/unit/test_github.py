"""GitHub and CI building blocks: signatures, events, selection, the API client,
what is written back, CI outcomes, and the command-line script. No database, no network."""

import importlib.util
import json
import re
import threading
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.integrations.github import webhooks
from app.integrations.github.client import AppAuth, GitHubClient, GitHubError, TokenAuth
from app.integrations.github.reporting import (
    MARKER,
    RunSummary,
    code,
    commit_status,
    md,
    pr_comment,
    status_context,
)
from app.integrations.github.selection import PathRule, path_matches, select
from app.reporting.ci import ci_outcome, to_junit
from app.services.api_keys import generate_key, hash_key
from tests.unit.test_reporting import sample_report

SECRET = "it-is-a-webhook-secret-of-some-length"
TOKEN = "github_pat_11AAAAAAA0_secretsecretsecretsecretsecret"

# --- webhook signatures -------------------------------------------------------------------


def test_only_the_sha256_signature_of_exactly_these_bytes_is_accepted() -> None:
    body = b'{"zen":"Keep it logically awesome."}'
    good = webhooks.sign(SECRET, body)
    assert good.startswith("sha256=") and webhooks.verify_signature(SECRET, body, good)

    refused = [
        None,
        "",
        good[:-1],
        good.upper(),
        good.replace("sha256=", "sha1="),
        good.removeprefix("sha256="),
        "sha256=" + "0" * 64,
        webhooks.sign("another-secret", body),
        webhooks.sign(SECRET, body + b" "),  # one byte more
        webhooks.sign(SECRET, json.dumps(json.loads(body)).encode()),  # re-serialised
        good + "\n",
        "sha256=" + good.removeprefix("sha256=") + "00",
    ]
    for header in refused:
        assert not webhooks.verify_signature(SECRET, body, header), header
    # No secret, no acceptance: an empty secret would make every signature guessable.
    assert not webhooks.verify_signature("", body, webhooks.sign("", body))


def test_delivery_and_event_headers_must_look_like_githubs() -> None:
    assert webhooks.valid_delivery_id("72d3162e-cc78-11e3-81ab-4c9367dc0958")
    for bad in (None, "", "short", "a" * 200, "with space-0000", "../../etc/passwd-0000"):
        assert not webhooks.valid_delivery_id(bad)
    assert webhooks.valid_event("pull_request") and not webhooks.valid_event("Pull-Request!")


# --- reading events -----------------------------------------------------------------------

SHA = "a" * 40


def pr_payload(action: str = "opened", **over: Any) -> bytes:
    pull = {
        "number": 7, "state": "open", "draft": False, "title": "Change rounding", "user": {"login": "ada"},
        "head": {"sha": SHA, "ref": "feature/rounding", "repo": {"id": 100}},
        "base": {"ref": "main", "repo": {"id": 100}},
    }  # fmt: skip
    pull.update(over.pop("pull", {}))
    return json.dumps(
        {"action": action, "repository": {"id": 100}, "pull_request": pull, **over}
    ).encode()


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened", "ready_for_review"])
def test_pull_request_actions_that_start_tests(action: str) -> None:
    event = webhooks.parse_event("pull_request", pr_payload(action))
    assert event.triggers and (event.kind, event.action, event.repository_id) == (
        "pull_request",
        action,
        100,
    )
    assert event.subject == {
        "pull_request": 7, "head_sha": SHA, "head_ref": "feature/rounding", "base_ref": "main",
        "draft": False, "from_fork": False, "title": "Change rounding", "author": "ada",
    }  # fmt: skip


@pytest.mark.parametrize("action", ["closed", "labeled", "edited", "review_requested", "assigned"])
def test_other_pull_request_actions_do_not(action: str) -> None:
    event = webhooks.parse_event("pull_request", pr_payload(action))
    assert not event.triggers and "does not start tests" in (event.reason or "")


def test_incomplete_malformed_and_unused_events_are_ignored_not_guessed_at() -> None:
    for name, body in [
        ("pull_request", b"not json"),
        ("pull_request", b"[]"),
        ("pull_request", b'{"action":"opened"}'),
        ("pull_request", pr_payload(pull={"head": {"sha": "not-a-sha"}})),
        ("pull_request", pr_payload(pull={"number": "7; drop table"})),
        ("pull_request", pr_payload(pull={"state": "closed"})),
        ("push", b'{"ref":"refs/heads/main"}'),
        ("issues", b'{"action":"opened","repository":{"id":100}}'),
        ("workflow_run", b"{}"),
    ]:
        event = webhooks.parse_event(name, body)
        assert (event.kind, event.triggers) == ("ignored", False) and event.reason, (name, body)
    fork = webhooks.parse_event(
        "pull_request", pr_payload(pull={"head": {"sha": SHA, "ref": "x", "repo": {"id": 999}}})
    )
    assert fork.subject["from_fork"] is True
    assert webhooks.parse_event("ping", b'{"zen":"x","repository":{"id":100}}').kind == "ping"


def test_pushes_to_branches_start_tests_and_tags_and_deletions_do_not() -> None:
    def push(**over: Any) -> webhooks.Event:
        body = {"ref": "refs/heads/main", "before": "b" * 40, "after": SHA, "repository": {"id": 100},
                "pusher": {"name": "ada"}, **over}  # fmt: skip
        return webhooks.parse_event("push", json.dumps(body).encode())

    event = push()
    assert event.triggers and event.subject["branch"] == "main" and event.subject["head_sha"] == SHA
    assert not push(ref="refs/tags/v1.0").triggers
    assert not push(deleted=True).triggers
    assert not push(after="0" * 40).triggers


def test_branches_match_by_name_or_pattern_and_never_by_default() -> None:
    assert webhooks.branch_matches("main", ["main"])
    assert webhooks.branch_matches("release/2.1", ["main", "release/*"])
    assert not webhooks.branch_matches("main-backup", ["main"])
    assert not webhooks.branch_matches("Main", ["main"])
    assert not webhooks.branch_matches("feature/x", ["main", "release/*"])
    assert not webhooks.branch_matches("main", [])


# --- test selection -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("src/billing/invoice.py", "src/billing/**", True),
        ("src/billing/tax/vat.py", "src/billing/**", True),
        ("src/billing/tax/vat.py", "src/billing/", True),
        ("src/billing_old/x.py", "src/billing/**", False),
        ("src/billing/invoice.py", "src/*/invoice.py", True),
        ("src/a/b/invoice.py", "src/*/invoice.py", False),
        ("src/a/b/invoice.py", "src/**/invoice.py", True),
        ("src/invoice.py", "src/**/invoice.py", True),
        ("docs/guide/intro.md", "*.md", True),
        ("README.md", "*.md", True),
        ("docs/readme.mdx", "*.md", False),
        ("src/billing/invoice.py", "billing/**", False),  # anchored at the root
        ("anything/at/all.txt", "**", True),
        ("SRC/Billing/x.py", "src/billing/**", False),
    ],
)
def test_path_patterns(path: str, pattern: str, expected: bool) -> None:
    assert path_matches(path, pattern) is expected


BILLING, CHECKOUT, SMOKE = "suite-billing", "suite-checkout", "suite-smoke"
RULES = [
    PathRule("src/billing/**", (BILLING,)),
    PathRule("src/checkout/**", (CHECKOUT,)),
    PathRule("src/shared/**", (BILLING, CHECKOUT)),
]
FULL = [BILLING, CHECKOUT, SMOKE]


def choose(
    files: list[str] | None, *, complete: bool = True, mode: str = "affected", rules: Any = RULES
) -> Any:
    return select(
        mode=mode, full_suite_ids=FULL, rules=rules, changed_files=files, complete=complete
    )


def test_a_run_is_narrowed_only_when_every_changed_file_is_covered_by_a_rule() -> None:
    found = choose(["src/billing/invoice.py", "src/billing/tax/vat.py"])
    assert (found.scope, found.suite_ids) == ("affected", [BILLING])
    assert (
        found.matched == {BILLING: ["src/billing/**"]}
        and found.as_dict()["decided_by"] == "path rules"
    )

    both = choose(["src/billing/a.py", "src/checkout/b.py"])
    assert (both.scope, both.suite_ids) == ("affected", [BILLING, CHECKOUT])
    shared = choose(["src/shared/money.py"])
    assert shared.suite_ids == [BILLING, CHECKOUT]


@pytest.mark.parametrize(
    ("files", "kwargs", "why"),
    [
        (["src/billing/a.py", "infra/deploy.yml"], {}, "not covered by any path rule"),
        (["package-lock.json"], {}, "not covered by any path rule"),
        (["src/billing/a.py"], {"complete": False}, "may be incomplete"),
        (None, {}, "could not be read"),
        ([], {}, "No changed files"),
        (["src/billing/a.py"], {"mode": "all"}, "always runs the full set"),
        (["src/billing/a.py"], {"rules": []}, "No path rules"),
        (
            ["x/y.py"],
            {"rules": [PathRule("x/**", ("suite-of-another-repository",))]},
            "no suite of this repository",
        ),
    ],
)
def test_whenever_the_impact_is_uncertain_everything_runs(
    files: Any, kwargs: dict[str, Any], why: str
) -> None:
    found = choose(files, **kwargs)
    assert (found.scope, found.suite_ids) == ("full", FULL)
    assert why in found.reason


def test_an_unmatched_file_is_named_and_selection_never_returns_nothing() -> None:
    found = choose(["src/billing/a.py", "infra/deploy.yml", "Makefile"])
    assert (
        found.unmatched == ["infra/deploy.yml", "Makefile"]
        and found.as_dict()["unmatched_count"] == 2
    )
    for files in (["src/billing/a.py"], ["zzz"], [], None):
        assert choose(files).suite_ids, files


# --- the API client -----------------------------------------------------------------------


def client_for(handler: Any, auth: Any = None) -> GitHubClient:
    return GitHubClient(auth or TokenAuth(TOKEN), transport=httpx.MockTransport(handler))


async def test_every_request_carries_the_token_in_the_header_only_and_names_the_api_version() -> (
    None
):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": 100, "full_name": "acme/shop", "default_branch": "main",
                                         "private": True, "permissions": {"pull": True, "push": False}})  # fmt: skip

    async with client_for(handler) as client:
        repository = await client.repository("acme/shop")

    request = seen[0]
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["x-github-api-version"] == "2022-11-28"
    assert request.headers["accept"] == "application/vnd.github+json"
    assert TOKEN not in str(request.url) and request.url.host == "api.github.com"
    assert (repository.id, repository.private, repository.permissions) == (
        100,
        True,
        {"pull": True, "push": False},
    )


@pytest.mark.parametrize(
    ("status", "headers", "expected", "transient"),
    [
        (401, {}, "unauthorized", False),
        (403, {}, "forbidden", False),
        (403, {"x-ratelimit-remaining": "0"}, "rate_limited", True),
        (403, {"retry-after": "60"}, "rate_limited", True),
        (429, {}, "rate_limited", True),
        (404, {}, "not_found", False),
        (422, {}, "rejected", False),
        (500, {}, "unavailable", True),
        (502, {}, "unavailable", True),
        (302, {"location": "https://evil.example/steal"}, "unexpected", False),
    ],
)
async def test_github_failures_become_errors_that_mention_no_credential(
    status: int, headers: dict[str, str], expected: str, transient: bool
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(status, headers=headers, json={"message": f"Bad credentials {TOKEN}"})

    async with client_for(handler) as client:
        with pytest.raises(GitHubError) as raised:
            await client.repository("acme/shop")

    error = raised.value
    assert (error.code, error.transient) == (expected, transient)
    # GitHub's own text is not passed on, and the token appears nowhere.
    assert (
        TOKEN not in str(error) and TOKEN not in repr(error) and "Bad credentials" not in str(error)
    )
    # A redirect is not followed: the token is never sent to another host.
    assert calls == ["api.github.com"]


async def test_timeouts_unreachable_hosts_and_unreadable_answers_are_errors_too() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect with {TOKEN}", request=request)

    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>proxy error</html>")

    for handler, expected in (
        (timeout, "timeout"),
        (refused, "unavailable"),
        (garbage, "invalid_response"),
    ):
        async with client_for(handler) as client:
            with pytest.raises(GitHubError) as raised:
                await client.repository("acme/shop")
        assert raised.value.code == expected and TOKEN not in str(raised.value)
    assert "github_pat" not in repr(TokenAuth(TOKEN))


async def test_changed_files_are_paged_include_old_names_and_say_when_they_may_be_incomplete() -> (
    None
):
    pages = {
        "1": [{"filename": f"src/f{n}.py"} for n in range(100)],
        "2": [{"filename": "src/new_name.py", "previous_filename": "src/old_name.py"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["per_page"] == "100"
        return httpx.Response(200, json=pages.get(request.url.params["page"], []))

    async with client_for(handler) as client:
        files = await client.pull_request_files("acme/shop", 7)
    assert len(files.paths) == 102 and files.complete
    assert files.paths[-2:] == ["src/new_name.py", "src/old_name.py"]

    def endless(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[{"filename": f"f{request.url.params['page']}-{n}"} for n in range(100)]
        )

    async with client_for(endless) as client:
        capped = await client.pull_request_files("acme/shop", 7)
    assert len(capped.paths) == 3000 and capped.complete is False


async def test_a_github_app_exchanges_a_signed_jwt_for_an_installation_token_and_reuses_it() -> (
    None
):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/app/installations/42/access_tokens":
            expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
            return httpx.Response(
                201, json={"token": "ghs_installation_token", "expires_at": expires}
            )
        return httpx.Response(200, json={"id": 100, "full_name": "acme/shop"})

    auth = AppAuth("12345", pem, 42)
    async with client_for(handler, auth) as client:
        await client.repository("acme/shop")
        await client.repository("acme/shop")

    exchange = seen[0]
    claims = jwt.decode(
        exchange.headers["authorization"].removeprefix("Bearer "),
        key.public_key(),
        algorithms=["RS256"],
    )
    assert claims["iss"] == "12345" and claims["exp"] - claims["iat"] <= 600
    assert [r.url.path for r in seen] == [
        "/app/installations/42/access_tokens",
        "/repos/acme/shop",
        "/repos/acme/shop",
    ]
    assert seen[1].headers["authorization"] == "Bearer ghs_installation_token"
    assert "PRIVATE KEY" not in repr(auth) and "ghs_" not in repr(auth)

    async with client_for(handler, AppAuth("12345", "not a key", 42)) as broken:
        with pytest.raises(GitHubError) as raised:
            await broken.repository("acme/shop")
    assert raised.value.code == "invalid_credentials"


# --- CI outcome ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "verdict", "outcome", "exit_code", "passed"),
    [
        ("completed", "passed", "passed", 0, True),
        ("completed", "failed", "failed", 1, False),
        ("completed", "error", "unverified", 2, False),
        ("completed", "inconclusive", "unverified", 2, False),
        ("completed", "flaky", "unverified", 2, False),
        ("completed", "needs_review", "unverified", 2, False),
        ("completed", "something_new", "unverified", 2, False),
        ("failed", None, "incomplete", 3, False),
        ("cancelled", "inconclusive", "incomplete", 3, False),
        (
            "timed_out",
            "passed",
            "incomplete",
            3,
            False,
        ),  # a run that did not finish is never a pass
        ("queued", None, "running", None, False),
        ("running", None, "running", None, False),
        ("cancelling", None, "running", None, False),
    ],
)
def test_only_a_completed_execution_with_verdict_passed_is_a_ci_success(
    status: str, verdict: str | None, outcome: str, exit_code: int | None, passed: bool
) -> None:
    found = ci_outcome(status, verdict, {"total": 3, "failed": 1})
    assert (found.outcome, found.exit_code, found.passed) == (outcome, exit_code, passed)
    assert found.finished is (outcome != "running") and found.summary


@pytest.mark.parametrize(
    ("status", "verdict", "state"),
    [
        ("completed", "passed", "success"),
        ("completed", "failed", "failure"),
        ("completed", "flaky", "failure"),
        ("completed", "needs_review", "failure"),
        ("completed", "error", "failure"),
        ("completed", "inconclusive", "failure"),
        ("cancelled", None, "error"),
        ("running", None, "pending"),
    ],
)
def test_the_commit_status_is_success_only_for_a_verified_pass(
    status: str, verdict: str | None, state: str
) -> None:
    assert commit_status(ci_outcome(status, verdict, {"total": 2, "failed": 1})).state == state
    assert status_context("Loyalty") == "QA Agent / Loyalty" and status_context(None) == "QA Agent"


def test_junit_counts_flaky_and_needs_review_as_failures_and_is_well_formed() -> None:
    report, _ = sample_report()
    hostile = report.model_copy(deep=True)
    hostile.results[1].title = "]]><script>alert(1)</script> \x00\x07 & <b>"
    hostile.results[1].failure_summary = 'expected "50" & found <51>'

    suite = ET.fromstring(to_junit(hostile))  # noqa: S314 - our own output

    assert suite.tag == "testsuite" and suite.attrib["name"] == "Loyalty"
    # One of each status: failed + flaky + needs_review are failures; skipped + blocked are skipped.
    assert (suite.attrib["tests"], suite.attrib["failures"], suite.attrib["errors"], suite.attrib["skipped"]) == (
        "7", "3", "1", "2",
    )  # fmt: skip
    cases = suite.findall("testcase")
    assert len(cases) == 7 and cases[0].find("failure") is None and cases[0].find("error") is None
    failure = cases[1].find("failure")
    assert failure is not None and failure.attrib["type"] == "test_assertion_failure"
    assert failure.attrib[
        "message"
    ] == 'expected "50" & found <51>' and "Expected: {'equals': '50'}" in (failure.text or "")
    assert "<script>" in cases[1].attrib["name"] and "\x00" not in cases[1].attrib["name"]
    assert cases[2].find("error") is not None
    assert [c.find("failure").attrib["type"] for c in cases[5:7]] == ["flaky", "needs_review"]  # type: ignore[union-attr]
    properties = {p.attrib["name"]: p.attrib["value"] for p in suite.iter("property")}
    assert properties["verdict"] == "failed" and properties["execution_id"] == str(
        report.execution.id
    )


# --- the pull request comment -------------------------------------------------------------


def comment_for(**changes: Any) -> str:
    report, _ = sample_report()
    edited = report.model_copy(deep=True)
    for path, value in changes.items():
        index, field = path.split("__")
        setattr(edited.results[int(index)], field, value)
    run = RunSummary(
        edited,
        ci_outcome("completed", "failed", edited.totals.model_dump()),
        "https://qa.example/r/1",
    )
    return pr_comment(
        [run], head_sha=SHA, selection={"scope": "affected", "reason": "Covered by a path rule."}
    )


def test_the_comment_gives_counts_failed_tests_and_a_link_that_needs_signing_in() -> None:
    body = comment_for()
    assert body.startswith(MARKER) and "QA Agent: Tests failed" in body and f"`{SHA[:12]}`" in body
    assert (
        "| Loyalty | ❌ failed | 7 | 1 | 1 | 1 | 1 | 3 | [Open](https://qa.example/r/1) |" in body
    )
    assert "### Failed tests" in body and "**TC\\-0002** Case 2 — failed" in body
    assert "- Category (by rules): test assertion failure" in body
    assert "- Expected: `{'equals': '50'}`" in body and "- Found: `{'text': '51'}`" in body
    assert (
        "Suggested debugging steps (AI-generated suggestion, not a confirmed root cause):" in body
    )
    assert "  - Check the rule\\." in body
    # The errored case has no AI suggestion: general next steps for its category.
    assert "- Suggested next steps:" in body and "renamed or removed element" in body
    assert "### Not counted as passed" in body and "passed only after a retry" in body
    assert "require signing in" in body and "Ran the suites this change concerns." in body
    assert "evidence" in body and "/evidence/" not in body  # no direct file addresses


def test_text_from_a_test_run_cannot_become_markup_a_link_or_a_mention() -> None:
    hostile = "@octocat [click](https://evil.example) ![x](https://evil.example/p.png) <img src=x> `code` | **b**"
    body = comment_for(**{"1__title": hostile, "1__failure_summary": hostile})
    # Every character that could start markup is escaped, so none of it is live.
    for live in (
        r"(?<!\\)\[click\]\(",
        r"(?<!\\)!\[x",
        r"(?<!\\)<img",
        r"(?<!\\)@octocat",
        r"(?<!\\)\*\*b",
    ):
        assert re.search(live, body.split("### Failed tests")[1].split("- Category")[0]) is None, (
            live
        )
    assert "\\<img src=x\\>" in body and "\\[click\\]\\(https://evil\\.example\\)" in body
    # A zero-width space after the @ keeps GitHub from notifying that account.
    assert "@\u200boctocat" in body and "@octocat" not in body
    assert md("@team `x` <b>") == "@\u200bteam \\`x\\` \\<b\\>"
    assert code("a `b` c") == "`a 'b' c`" and code("line1\nline2") == "`line1 line2`"
    assert len(md("x" * 1000)) == 301


def test_the_headline_is_the_worst_outcome_and_a_long_comment_is_cut() -> None:
    report, _ = sample_report()
    passed = RunSummary(report, ci_outcome("completed", "passed", {"total": 1}), None)
    running = RunSummary(report, ci_outcome("running", None), None)
    unverified = RunSummary(report, ci_outcome("completed", "flaky"), None)
    assert "All tests passed" in pr_comment([passed], head_sha=SHA)
    assert "Tests are running" in pr_comment([passed, running], head_sha=SHA)
    assert "did not give a verified pass" in pr_comment([passed, unverified], head_sha=SHA)
    many = report.model_copy(deep=True)
    many.results = [many.results[1].model_copy(update={"title": "T" * 150}) for _ in range(400)]
    long = pr_comment([RunSummary(many, ci_outcome("completed", "failed"), None)], head_sha=SHA)
    assert len(long) < 62_000 and "…and 390 more" in long


# --- API keys -----------------------------------------------------------------------------


def test_an_api_key_names_its_organization_and_only_its_hash_is_comparable() -> None:
    organization = uuid.uuid4()
    first, second = generate_key(organization), generate_key(organization)
    assert first != second and first.startswith(f"qak_{organization.hex}_") and len(first) > 70
    assert hash_key(first) != hash_key(second) and len(hash_key(first)) == 64
    assert first not in hash_key(first)


# --- the command-line script --------------------------------------------------------------

_SPEC = importlib.util.spec_from_file_location(
    "qa_agent_ci", Path(__file__).parents[2] / "ci" / "qa_agent_ci.py"
)
assert _SPEC is not None and _SPEC.loader is not None
cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cli)
KEY = "qak_" + "0" * 32 + "_" + "k" * 43


class StubServer:
    """Answers like the CI API: `polls` running answers, then `final`."""

    def __init__(self, final: dict[str, Any], *, polls: int = 1, start_status: int = 202) -> None:
        self.final, self.polls, self.start_status = final, polls, start_status
        self.requests: list[tuple[str, str, dict[str, str], Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                pass

            def _answer(self, status: int, body: bytes, kind: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _record(self) -> Any:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                outer.requests.append((self.command, self.path, dict(self.headers), body))
                return body

            def do_POST(self) -> None:
                self._record()
                if outer.start_status >= 400:
                    self._answer(
                        outer.start_status,
                        json.dumps(
                            {
                                "code": "suite_not_approved",
                                "detail": "Only an approved suite can be run.",
                            }
                        ).encode(),
                    )
                    return
                self._answer(
                    outer.start_status,
                    json.dumps(
                        {
                            "id": "e1",
                            "finished": False,
                            "outcome": "running",
                            "report_url": "https://qa/r",
                        }
                    ).encode(),
                )

            def do_GET(self) -> None:
                self._record()
                if self.path.endswith("/junit"):
                    self._answer(200, b"<testsuite/>", "application/xml")
                    return
                outer.polls -= 1
                running = {"id": "e1", "finished": False, "outcome": "running"}
                self._answer(200, json.dumps(running if outer.polls > 0 else outer.final).encode())

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def final(outcome: str, verdict: str, exit_code: int, **more: Any) -> dict[str, Any]:
    return {"id": "e1", "finished": True, "outcome": outcome, "verdict": verdict, "passed": outcome == "passed",
            "exit_code": exit_code, "summary": "s", "totals": {"total": 2, "passed": 1, "failed": 1},
            "failures": [{"case_key": "TC-0002", "title": "Points", "status": "failed", "failure_category": "test_assertion_failure"}],
            "report_url": "https://qa/r", **more}  # fmt: skip


ARGS = [
    "--project",
    "p1",
    "--environment",
    "env1",
    "--suite",
    "s1",
    "--poll",
    "0.01",
    "--allow-insecure",
]


@pytest.mark.parametrize(
    ("answer", "extra", "expected"),
    [
        (final("passed", "passed", 0), [], 0),
        (final("failed", "failed", 1), [], 1),
        (final("unverified", "flaky", 2), [], 2),
        (final("unverified", "flaky", 2), ["--allow-flaky"], 0),
        (final("unverified", "needs_review", 2), ["--allow-flaky"], 2),
        (final("unverified", "error", 2), ["--allow-flaky"], 2),
        (final("incomplete", "inconclusive", 3), [], 3),
        # Answers that do not add up are never a success.
        ({"id": "e1", "finished": True}, [], 2),
        (
            {"id": "e1", "finished": True, "passed": True, "outcome": "failed", "exit_code": 0},
            [],
            2,
        ),
        ({"id": "e1", "finished": True, "passed": "yes", "outcome": "passed"}, [], 2),
    ],
)
def test_the_script_exits_by_the_verified_outcome(
    answer: dict[str, Any],
    extra: list[str],
    expected: int,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = StubServer(answer, polls=2)
    try:
        junit, summary = tmp_path / "r.xml", tmp_path / "summary.md"
        env = {"QA_AGENT_URL": server.url, "QA_AGENT_API_KEY": KEY, "GITHUB_REPOSITORY": "acme/shop",
               "GITHUB_SHA": "c" * 40, "GITHUB_REF": "refs/pull/7/merge", "GITHUB_RUN_ID": "99",
               "GITHUB_SERVER_URL": "https://github.com", "GITHUB_STEP_SUMMARY": str(summary)}  # fmt: skip
        code = cli.main([*ARGS, *extra, "--junit", str(junit)], env)
    finally:
        server.close()

    assert code == expected
    method, path, headers, body = server.requests[0]
    assert (method, path) == ("POST", "/api/v1/ci/executions")
    assert (
        headers["Authorization"] == f"Bearer {KEY}" and headers["Idempotency-Key"] == "gha-99-1-s1"
    )
    assert body == {"project_id": "p1", "environment_id": "env1", "suite_id": "s1",
                    "git": {"repository": "acme/shop", "sha": "c" * 40, "ref": "refs/pull/7/merge",
                            "pull_request": 7, "run_url": "https://github.com/acme/shop/actions/runs/99"}}  # fmt: skip
    assert junit.read_bytes() == b"<testsuite/>" and f"Exit code {expected}." in summary.read_text()
    assert KEY not in capsys.readouterr().out and KEY not in summary.read_text()


def test_the_script_fails_closed_when_it_cannot_start_follow_or_reach_the_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = {"QA_AGENT_URL": "https://qa.example", "QA_AGENT_API_KEY": KEY}
    assert cli.main(ARGS[:6], {"QA_AGENT_URL": "https://qa.example"}) == 4  # no key
    assert cli.main(ARGS[:6], {"QA_AGENT_API_KEY": KEY}) == 4  # no address
    assert cli.main(ARGS[:6], {**env, "QA_AGENT_URL": "http://qa.example"}) == 4  # not https
    assert cli.main(["--project", "p", "--environment", "e"], env) == 4  # nothing to run
    assert cli.main([*ARGS[:6], "--case", "c1"], env) == 4  # both a suite and cases
    assert (
        cli.main(ARGS, {"QA_AGENT_URL": "http://127.0.0.1:9", "QA_AGENT_API_KEY": KEY}) == 4
    )  # unreachable

    refused = StubServer({}, start_status=409)
    never = StubServer({"id": "e1", "finished": False}, polls=10_000)
    try:
        assert cli.main(ARGS, {"QA_AGENT_URL": refused.url, "QA_AGENT_API_KEY": KEY}) == 4
        assert (
            cli.main(
                [*ARGS, "--timeout", "0"], {"QA_AGENT_URL": never.url, "QA_AGENT_API_KEY": KEY}
            )
            == 4
        )
    finally:
        refused.close()
        never.close()
    output = capsys.readouterr().out
    assert "suite_not_approved" in output and "did not finish within 0 seconds" in output
    assert KEY not in output
    # The key cannot be given on the command line at all.
    with pytest.raises(SystemExit):
        cli.parser().parse_args([*ARGS, "--api-key", KEY])


def test_git_details_come_from_the_pipeline_environment_and_are_omitted_when_absent() -> None:
    assert cli.git_from_environment({}) == {}
    assert cli.git_from_environment(
        {"GITHUB_SHA": "not a sha", "GITHUB_REF": "refs/heads/main"}
    ) == {"ref": "refs/heads/main"}
    preferred = cli.git_from_environment({"GITHUB_SHA": "a" * 40, "QA_AGENT_SHA": "b" * 40})
    assert preferred == {"sha": "b" * 40}
    assert cli.default_idempotency_key({}, "s1") is None
