"""The real thing: Playwright MCP, a real browser, the egress proxy and the demo app.

    make be-test-browser

These tests are what ties the permission table and the result parsing to the
pinned server version. If an upgrade of Playwright MCP changes tool names,
arguments or the layout of results, they fail here.
"""

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.browser.errors import (
    BrowserError,
    BrowserTimeoutError,
    DownloadBlockedError,
    LoginFailedError,
    ScopeViolationError,
    SessionClosedError,
)
from app.browser.login import LoginRecipe, run_login
from app.browser.manager import BrowserSessionManager, SessionReport, SessionSpec
from app.browser.policy import PLAYWRIGHT_MCP_RULES, SessionGrant
from app.browser.services import EgressClient, LaunchOptions, RunnerClient, Viewport
from app.browser.session import BrowserSession
from app.core.security.url_safety import DestinationPolicy, parse_origin
from app.models.project import BROWSER_ACTIONS
from tests.conftest import Api
from tests.mcp_fakes import Trace

pytestmark = pytest.mark.browser

DEMO = "http://demo-app:8080"
USERNAME, PASSWORD = "demo", "demo-password"
RECIPE_STEPS = [
    {"action": "goto", "path": "/login"},
    {
        "action": "fill",
        "target": {"role": "textbox", "name": "Username"},
        "value": "{{cred.username}}",
    },
    {
        "action": "fill",
        "target": {"role": "textbox", "name": "Password"},
        "value": "{{cred.password}}",
    },
    {"action": "click", "target": {"role": "button", "name": "Sign in"}},
    {"action": "expect", "text": "Signed in as", "url_path": "/account"},
]
RECIPE = LoginRecipe.model_validate(RECIPE_STEPS)


def _runner() -> RunnerClient:
    return RunnerClient(os.environ["RUNNER_URL"], os.environ["RUNNER_TOKEN"])


def real_manager() -> BrowserSessionManager:
    return BrowserSessionManager(
        _runner(),
        EgressClient(
            os.environ["EGRESS_ADMIN_URL"],
            os.environ["EGRESS_ADMIN_TOKEN"],
            os.environ["EGRESS_PROXY_URL"],
        ),
    )


class Real:
    def __init__(self) -> None:
        self.trace = Trace()
        self.report = SessionReport()

    @asynccontextmanager
    async def open(
        self, *origins: str, private: bool = True, navigation_timeout_ms: int = 15_000, **grant: Any
    ) -> AsyncIterator[BrowserSession]:
        grant.setdefault("allowed_actions", frozenset(BROWSER_ACTIONS))
        spec = SessionSpec(
            organization_id=uuid.uuid4(),
            destinations=DestinationPolicy(
                allowed_origins=frozenset(parse_origin(o) for o in (origins or (DEMO,))),
                allow_private_network=private,
            ),
            grant=SessionGrant(**grant),
            options=LaunchOptions(
                viewport=Viewport(1024, 768), navigation_timeout_ms=navigation_timeout_ms, ttl_s=120
            ),
        )
        async with real_manager().open(spec, self.trace, report=self.report) as session:
            yield session


# --- the server matches what the code was written against ---------------------------------


async def test_pinned_server_matches_the_permission_table() -> None:
    real = Real()
    async with real.open():
        pass

    assert real.report.server_name == "Playwright"
    assert (await _runner().health())["mcp_version"] == "0.0.86"
    discovered = {tool["name"] for tool in real.report.discovered}
    # Every rule refers to a tool this server really has, with the expected arguments.
    assert {rule.name for rule in PLAYWRIGHT_MCP_RULES} <= discovered
    assert "tool_contract_mismatch" not in real.report.withheld.values()
    assert "tool_schema_invalid" not in real.report.withheld.values()
    # Tools without a rule are withheld. A new name here means: classify it on purpose.
    unknown = {name for name, code in real.report.withheld.items() if code == "tool_unknown"}
    assert unknown == {"browser_emulate_media"}
    assert "browser_evaluate" not in real.report.exposed
    assert "browser_run_code_unsafe" not in real.report.exposed


# --- the browser tools a test workflow needs ----------------------------------------------


async def test_navigate_inspect_fill_select_click_dialog_screenshot_console_and_tabs() -> None:
    real = Real()
    async with real.open() as browser:
        home = await browser.call("browser_navigate", {"url": f"{DEMO}/"})
        assert (home.page_url, home.page_title) == (f"{DEMO}/", "Demo shop")

        snapshot = await browser.call("browser_snapshot")
        assert 'link "Order form"' in snapshot.text
        found = await browser.call("browser_find", {"text": "Sign in"})
        assert "Sign in" in found.text

        await browser.call("browser_click", {"target": 'role=link[name="Order form"]'})
        await browser.call(
            "browser_select_option",
            {"target": 'role=combobox[name="Product"]', "values": ["Coffee"]},
        )
        await browser.call(
            "browser_type", {"target": 'role=spinbutton[name="Quantity"]', "text": "3"}
        )
        placed = await browser.call("browser_click", {"target": 'role=button[name="Place order"]'})
        assert placed.page_title == "Order placed"
        assert "Ordered 3 x coffee" in (await browser.call("browser_snapshot")).text

        await browser.call("browser_navigate", {"url": f"{DEMO}/dialog"})
        clicked = await browser.call(
            "browser_click", {"target": 'role=button[name="Delete basket"]'}
        )
        assert clicked.dialog_open
        await browser.call("browser_handle_dialog", {"accept": True})
        assert "Basket deleted" in (await browser.call("browser_snapshot")).text

        console = await browser.call("browser_console_messages", {"level": "error"})
        assert "demo console error" in console.text
        shot = await browser.call("browser_take_screenshot", {"scale": "css"})
        assert shot.images and shot.images[0].mime_type == "image/png"
        tabs = await browser.call("browser_tabs", {"action": "new", "url": f"{DEMO}/"})
        assert tabs.page_url == f"{DEMO}/"

        assert browser.stats.failed == 0 and browser.stats.refused == 0
    assert all(event.status == "ok" for event in real.trace.calls)


# --- authentication -----------------------------------------------------------------------


async def test_login_with_a_recipe_and_the_password_stays_out_of_everything_recorded() -> None:
    real = Real()
    async with real.open() as browser:
        result = await run_login(
            browser,
            base_url=DEMO,
            recipe=RECIPE,
            fields={"username": USERNAME, "password": PASSWORD},
        )
        account = await browser.call("browser_snapshot")

    assert result.steps_performed == 5 and result.page_url == f"{DEMO}/account"
    assert "Signed in as demo" in account.text
    assert PASSWORD not in real.trace.dump()
    assert PASSWORD not in account.text


async def test_wrong_password_is_a_failed_login() -> None:
    real = Real()
    async with real.open(navigation_timeout_ms=5_000) as browser:
        with pytest.raises(LoginFailedError):
            await run_login(
                browser,
                base_url=DEMO,
                recipe=RECIPE,
                fields={"username": USERNAME, "password": "wrong-password"},
            )
    assert "wrong-password" not in real.trace.dump()


async def test_sessions_do_not_share_cookies_or_login_state() -> None:
    first, second = Real(), Real()
    async with first.open() as signed_in, second.open() as other:
        await run_login(
            signed_in,
            base_url=DEMO,
            recipe=RECIPE,
            fields={"username": USERNAME, "password": PASSWORD},
        )
        mine = await signed_in.call("browser_navigate", {"url": f"{DEMO}/account"})
        theirs = await other.call("browser_navigate", {"url": f"{DEMO}/account"})

        assert mine.page_url == f"{DEMO}/account"
        assert theirs.page_url == f"{DEMO}/login"  # sent to sign in: no session cookie here
        assert (await _runner().health())["sessions"] >= 2


# --- staying inside the allowed origins ---------------------------------------------------


async def test_redirect_to_an_origin_that_is_not_allowed_is_stopped_by_the_proxy() -> None:
    real = Real()
    async with real.open() as browser:
        with pytest.raises((ScopeViolationError, BrowserError)) as raised:
            await browser.call(
                "browser_navigate", {"url": f"{DEMO}/redirect?to=http://example.com/"}
            )
        assert raised.value.code in (
            "left_allowed_origins",
            "destination_not_allowed",
            "navigation_failed",
        )
        if browser.state == "failed":
            with pytest.raises(SessionClosedError):
                await browser.call("browser_snapshot")

    blocked = {(entry["host"], entry["reason"]) for entry in real.report.blocked_requests}
    assert ("example.com", "origin_not_allowlisted") in blocked
    assert real.trace.calls[0].status != "ok"


async def test_platform_services_are_unreachable_even_if_an_environment_allowlists_them() -> None:
    """An admin who allowlists the runner itself and enables private addresses still gets nothing."""
    real = Real()
    async with real.open(DEMO, "http://runner:8900", "http://egress-proxy:8901") as browser:
        for url in ("http://runner:8900/livez", "http://egress-proxy:8901/livez"):
            with pytest.raises(BrowserError) as raised:
                await browser.call("browser_navigate", {"url": url})
            assert raised.value.code == "navigation_failed"
        assert browser.stats.performed == 0

    reasons = {(entry["host"], entry["reason"]) for entry in real.report.blocked_requests}
    assert {("runner", "platform_address"), ("egress-proxy", "platform_address")} <= reasons


async def test_page_content_cannot_direct_the_session_and_downloads_are_refused() -> None:
    real = Real()
    async with real.open() as browser:
        notice = await browser.call("browser_navigate", {"url": f"{DEMO}/injection"})
        # The page asks to be taken elsewhere. Nothing reads page text as instructions,
        # and following it would be refused anyway.
        assert notice.page_url == f"{DEMO}/injection"
        with pytest.raises(ScopeViolationError):
            await browser.call("browser_navigate", {"url": "http://attacker.invalid/steal"})

        with pytest.raises(DownloadBlockedError):
            await browser.call("browser_navigate", {"url": f"{DEMO}/download"})


# --- failures -----------------------------------------------------------------------------


async def test_slow_page_times_out_as_a_failure() -> None:
    real = Real()
    async with real.open(navigation_timeout_ms=2_000) as browser:
        with pytest.raises(BrowserTimeoutError):
            await browser.call("browser_navigate", {"url": f"{DEMO}/slow?seconds=20"})
        assert browser.stats.performed == 0
    assert real.trace.calls[0].status == "timeout"


async def test_sessions_are_destroyed_when_done_even_after_an_error() -> None:
    before = (await _runner().health())["sessions"]
    real = Real()
    with pytest.raises(RuntimeError):
        async with real.open() as browser:
            await browser.call("browser_navigate", {"url": f"{DEMO}/"})
            assert (await _runner().health())["sessions"] == before + 1
            raise RuntimeError("the job crashed")
    assert (await _runner().health())["sessions"] == before
    assert real.trace.sessions[-1].kind == "closed"


# --- through the API and the worker job ---------------------------------------------------


@pytest.mark.integration
async def test_browser_check_end_to_end_with_stored_credential(api: Api) -> None:
    api.browser_manager = real_manager()
    api.app.state.settings = api.app.state.settings.model_copy(
        update={"runner_url": os.environ["RUNNER_URL"]}
    )
    api.network.hosts["demo-app"] = ["10.0.0.9"]
    owner = await api.register("owner@acme.io")
    project = await owner.create_project()
    application = await owner.create_application(project["id"], base_url=DEMO)
    environment = await owner.create_environment(
        project["id"], application["id"], name="demo", kind="development", base_url=DEMO,
        allow_private_network=True, is_read_only=False, requires_approval=False,
    )  # fmt: skip
    environment_url = (
        f"{owner.projects_url}/{project['id']}/applications/{application['id']}"
        f"/environments/{environment['id']}"
    )
    created = await owner.post(
        f"{environment_url}/credentials",
        json={
            "name": "customer", "role_label": "customer", "kind": "username_password",
            "secret": {"username": USERNAME, "password": PASSWORD},
            "is_test_account": True, "login_recipe": RECIPE_STEPS,
        },
    )  # fmt: skip
    assert created.status_code == 201, created.text

    started = await owner.post(
        f"{environment_url}/browser-checks", json={"credential_id": created.json()["id"]}
    )
    assert started.status_code == 202, started.text
    assert await api.run_jobs() == ["passed"]
    check = (await owner.get(f"{environment_url}/browser-checks/{started.json()['id']}")).json()

    assert check["status"] == "passed" and check["result"]["logged_in"] is True
    assert check["result"]["page_url"] == f"{DEMO}/account"
    assert check["session"]["server"] == "Playwright"
    assert PASSWORD not in str(check)
    status = (await owner.get(f"/api/v1/organizations/{owner.org_id}/browser")).json()
    assert status["connection"]["status"] == "active"
    assert status["connection"]["unknown_tools"] == ["browser_emulate_media"]
