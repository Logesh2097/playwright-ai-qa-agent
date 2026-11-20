"""End-to-end walk through the web application against a running stack.

Uses a real browser. Registers a new account through the registration page,
prepares a project through the API, then visits every screen at a desktop and a
tablet width, starts an execution from the suite page, watches it live, and
opens and exports its report.

    make e2e            (the stack and the demo application must be running)

Exits non-zero if a screen shows an error state, a page logs a script error, a
screen scrolls sideways, or a step does not behave as expected.
"""

import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
from playwright.async_api import Page, async_playwright, expect

WEB = os.environ.get("E2E_WEB_URL", "http://localhost:5173")
API = os.environ.get("E2E_API_URL", "http://localhost:8010") + "/api/v1"
DEMO = os.environ.get("E2E_DEMO_URL", "http://demo-app:8080")
OUT = Path(os.environ.get("E2E_OUT", "output"))
EMAIL = f"e2e-{uuid.uuid4().hex[:8]}@walkthrough.io"
PASSWORD = "Zx9!vK-" + uuid.uuid4().hex[:12]
VIEWPORTS = {"desktop": {"width": 1366, "height": 900}, "tablet": {"width": 820, "height": 1180}}
problems: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  ok   " if condition else "  FAIL ") + message)
    if not condition:
        problems.append(message)


def case_content(title: str, amount: str) -> dict[str, Any]:
    return {
        "title": title, "description": "Runs against the demo application.", "category": "business_logic",
        "test_type": "ui", "objective_id": None, "preconditions": [],
        "test_data": [{"name": "bill_amount", "value": amount, "description": None}],
        "steps": [
            {"action": "navigate", "description": "Open the loyalty page", "target": None, "value": "/loyalty", "side_effect": "none"},
            {"action": "fill", "description": "Enter the bill amount", "target": "the Bill amount field", "value": "{{data.bill_amount}}", "side_effect": "none"},
            {"action": "click", "description": "Calculate the points", "target": "the Calculate points button", "value": None, "side_effect": "none"},
            {"action": "assert_text", "description": "Check the points", "target": "the points earned", "value": str(int(amount) // 10), "side_effect": "none"},
        ],
        "expected_result": f"{int(amount) // 10} points are shown", "expected_source": "human_specified",
        "priority": "p1", "severity": "major", "tags": [], "dependencies": [],
        "automation_feasibility": "automatable", "automation_notes": None,
        "verification": {"method": "ui_assertion", "description": "One point per 10 spent, rounded down"},
        "assumptions": [],
    }  # fmt: skip


def script(path: str) -> dict[str, Any]:
    return {"steps": [
        {"action": "navigate", "url": path},
        {"action": "fill", "locator": {"by": "label", "value": "Bill amount"}, "value": "{{data.bill_amount}}"},
        {"action": "click", "locator": {"by": "role", "role": "button", "name": "Calculate points"}},
        {"action": "read_text", "locator": {"by": "test_id", "value": "points"}, "save_as": "points", "as_number": True},
        {"action": "assert_expression", "description": "One point for every 10 spent, rounded down",
         "expression": "points == floor(number(data.bill_amount) / 10)"},
    ]}  # fmt: skip


async def prepare(token: str, org: str) -> dict[str, str]:
    """A project with an application, an environment and an approved suite of two cases."""
    async with httpx.AsyncClient(timeout=60, headers={"Authorization": f"Bearer {token}"}) as c:
        async def post(url: str, **body: Any) -> Any:
            r = await c.post(url, json=body or None)
            assert r.status_code < 300, (url, r.status_code, r.text[:300])
            return r.json()

        base = f"{API}/organizations/{org}/projects"
        project = await post(base, name="Demo shop")
        b = f"{base}/{project['id']}"
        app = await post(f"{b}/applications", name="Demo app", base_url=DEMO)
        env = await post(f"{b}/applications/{app['id']}/environments", name="demo", kind="development", base_url=DEMO,
                         allow_private_network=True, is_read_only=False, requires_approval=False)  # fmt: skip
        ids = []
        for title, path, amount in (("Points at 500", "/loyalty", "500"), ("Points with the rounding defect", "/loyalty?variant=rounding", "506")):
            case = await post(f"{b}/test-cases", application_id=app["id"], environment_id=env["id"], content=case_content(title, amount))
            r = await c.put(f"{b}/test-cases/{case['id']}/automation", json=script(path))
            assert r.status_code == 200, r.text[:300]
            await post(f"{b}/test-cases/{case['id']}/approve")
            ids.append(case["id"])
        suite = await post(f"{b}/test-suites", name="Loyalty", application_id=app["id"], case_ids=ids)
        await post(f"{b}/test-suites/{suite['id']}/approve")
        return {"project": project["id"], "application": app["id"], "suite": suite["id"], "case": ids[1]}


async def visit(page: Page, name: str, path: str, heading: str, size: str) -> None:
    await page.goto(WEB + path)
    await page.wait_for_load_state("networkidle")
    shown = await page.get_by_role("heading", level=1).first.text_content() or ""
    check(heading.lower() in shown.lower(), f"{size}: {name} shows “{heading}”")
    check(await page.locator(".error-state, [class*='error-state']").count() == 0, f"{size}: {name} has no error state")
    wider = await page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    check(wider <= 1, f"{size}: {name} does not scroll sideways ({wider}px)")
    await page.screenshot(path=str(OUT / f"{size}-{name}.png"), full_page=True)


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox"])
        context = await browser.new_context(viewport=VIEWPORTS["desktop"], accept_downloads=True)
        page = await context.new_page()
        script_errors: list[str] = []
        page.on("pageerror", lambda error: script_errors.append(str(error)))

        print("Account")
        await page.goto(f"{WEB}/login")
        await page.get_by_role("link", name="Forgot your password?").click()
        await expect(page.get_by_role("heading", name="Reset your password")).to_be_visible()
        await page.get_by_label("Email").fill("nobody@walkthrough.io")
        await page.get_by_role("button", name="Send reset link").click()
        sent = page.get_by_role("heading", name="Check your email")
        limited = page.get_by_text("Too many requests")
        await expect(sent.or_(limited)).to_be_visible()
        # Repeated runs from one address reach the rate limit, which is also a correct answer.
        check(True, "password reset request is accepted without saying whether the address exists"
              if await sent.count() else "password reset requests are rate limited")
        await page.goto(f"{WEB}/reset-password")
        await expect(page.get_by_role("heading", name="This link is not complete")).to_be_visible()

        await page.goto(f"{WEB}/register")
        await page.get_by_label("Your name").fill("Walk Through")
        await page.get_by_label("Work email").fill(EMAIL)
        await page.get_by_label("Password").first.fill(PASSWORD)
        org_field = page.get_by_label("Organization")
        if await org_field.count():
            await org_field.first.fill("Walkthrough Org")
        await page.get_by_role("button", name="Create account").click()
        await page.wait_for_url(lambda url: "/register" not in url, timeout=15000)
        check(True, "registration signs the new account in")
        await expect(page.get_by_role("heading", name="Dashboard")).to_be_visible()
        await expect(page.get_by_text("No projects yet")).to_be_visible()
        check(True, "an empty organization shows an empty state, not zeros dressed as results")

        # A reload restores the session from the cookie; the token is not in web storage.
        await page.reload()
        await expect(page.get_by_role("heading", name="Dashboard")).to_be_visible()
        stored = await page.evaluate("JSON.stringify({...localStorage, ...sessionStorage})")
        check("eyJ" not in stored, "no access token in localStorage or sessionStorage")

        async with httpx.AsyncClient(timeout=30) as c:
            session = (await c.post(f"{API}/auth/login", json={"email": EMAIL, "password": PASSWORD})).json()
        org = session["memberships"][0]["organization_id"]
        ids = await prepare(session["access_token"], org)
        project = f"/projects/{ids['project']}"

        print("Run a suite from its page and watch it live")
        await page.goto(f"{WEB}{project}/suites/{ids['suite']}")
        await page.get_by_role("button", name="Run suite").click()
        await expect(page.get_by_role("heading", name="Start an execution")).to_be_visible()
        await page.get_by_role("button", name="Start", exact=True).click()
        await page.wait_for_url("**/live", timeout=15000)
        await expect(page.get_by_role("heading", name="Test cases")).to_be_visible()
        await page.screenshot(path=str(OUT / "desktop-execution-live-running.png"), full_page=True)
        await expect(page.get_by_role("link", name="Open the report")).to_be_visible(timeout=120000)
        events = await page.locator(".events li").count()
        check(events >= 10, f"the live view listed {events} entries reported by the engine")
        check(await page.get_by_text("2 of 2 test cases finished").count() == 1, "progress ends at 2 of 2")
        await page.screenshot(path=str(OUT / "desktop-execution-live-finished.png"), full_page=True)
        execution_id = page.url.rstrip("/").split("/")[-2]

        await page.get_by_role("link", name="Open the report").click()
        await expect(page.get_by_role("heading", name="Test report")).to_be_visible()
        body = await page.locator("main").inner_text()
        check("Failure summary" in body and "test assertion failure" in body, "the report shows the failure and its category")
        await page.get_by_role("button", name="Show").first.click()
        await expect(page.locator("img.shot__thumb")).to_be_visible()
        check(True, "the failure screenshot is shown after asking for it")
        async with page.expect_download() as download:
            await page.get_by_role("button", name="Export PDF").click()
        saved = OUT / "report.pdf"
        await (await download.value).save_as(saved)
        check(saved.read_bytes().startswith(b"%PDF-"), "PDF export downloads a PDF")
        async with page.expect_download() as download:
            await page.get_by_role("button", name="Export CSV").click()
        check((await download.value).suggested_filename.endswith(".csv"), "CSV export downloads a CSV")

        # A second run, so that there is something to compare.
        async with httpx.AsyncClient(timeout=60, headers={"Authorization": f"Bearer {session['access_token']}"}) as c:
            envs = (await c.get(f"{API}/organizations/{org}/projects/{ids['project']}/applications/{ids['application']}/environments")).json()
            second = (await c.post(f"{API}/organizations/{org}/projects/{ids['project']}/executions",
                                   json={"environment_id": envs[0]["id"], "suite_id": ids["suite"]})).json()  # fmt: skip
            for _ in range(120):
                state = (await c.get(f"{API}/organizations/{org}/projects/{ids['project']}/executions/{second['id']}")).json()
                if state["finished_at"]:
                    break
                await asyncio.sleep(1)

        screens = [
            ("home", "/", "Dashboard"),
            ("projects", "/projects", "Projects"),
            ("project", project, "Demo shop"),
            ("application", f"{project}/applications/{ids['application']}", "Demo app"),
            ("instructions", f"{project}/instructions", "instructions"),
            ("suites", f"{project}/suites", "Test suites"),
            ("suite", f"{project}/suites/{ids['suite']}", "Loyalty"),
            ("case", f"{project}/cases/{ids['case']}", "Points with the rounding defect"),
            ("case-history", f"{project}/cases/{ids['case']}/history", "Result history"),
            ("project-dashboard", f"{project}/dashboard", "Test dashboard"),
            ("executions", f"{project}/executions", "Execution history"),
            ("execution-live", f"{project}/executions/{execution_id}/live", "Execution"),
            ("report", f"{project}/executions/{execution_id}", "Test report"),
            ("compare", f"{project}/executions/compare?base={execution_id}&target={second['id']}", "Comparison"),
            ("heal-proposals", f"{project}/heal-proposals", "Replacement locators"),
            ("project-integrations", f"{project}/integrations", "GitHub repositories"),
            ("integrations-github", "/integrations?tab=github", "Integrations"),
            ("integrations-ci", "/integrations?tab=ci", "Integrations"),
            ("integrations-mcp", "/integrations?tab=mcp", "Integrations"),
            ("settings-profile", "/settings?tab=profile", "Settings"),
            ("settings-security", "/settings?tab=security", "Settings"),
            ("settings-organization", "/settings?tab=organization", "Settings"),
            ("settings-roles", "/settings?tab=roles", "Settings"),
            ("settings-audit", "/settings?tab=audit", "Settings"),
            ("system", "/system", "Overview"),
            ("not-found", "/no/such/page", "not found"),
        ]
        for size, viewport in VIEWPORTS.items():
            print(f"Every screen at {size} width ({viewport['width']}px)")
            await page.set_viewport_size(viewport)
            for name, path, heading in screens:
                await visit(page, name, path, heading, size)

        print("Dashboard figures, navigation and sign-out")
        await page.set_viewport_size(VIEWPORTS["desktop"])
        await page.goto(WEB + "/")
        await page.wait_for_load_state("networkidle")
        text = await page.locator("main").inner_text()
        check("Test executions" in text and "Frequently failing tests" in text and "Recent executions" in text, "home dashboard has its sections")
        check(await page.locator("svg.trend__plot").count() == 1, "the trend chart is drawn")
        await page.goto(f"{WEB}/settings?tab=audit")
        await page.wait_for_load_state("networkidle")
        check(await page.get_by_text("execution.start").count() >= 1, "the audit log lists the executions that were started")
        await page.set_viewport_size(VIEWPORTS["tablet"])
        await page.goto(WEB + "/projects")
        toggle = page.get_by_role("button", name="Toggle navigation")
        check(not await toggle.is_visible(), "tablet: the sidebar stays in view")
        # Narrower than a tablet in portrait, the sidebar becomes a drawer.
        await page.set_viewport_size({"width": 700, "height": 1000})
        await expect(toggle).to_be_visible()
        check(True, "narrow: the navigation is behind a menu button")
        await toggle.click()
        await page.get_by_role("link", name="Settings").click()
        await expect(page.get_by_role("heading", name="Settings")).to_be_visible()
        await page.get_by_role("button", name="Sign out").click()
        await page.wait_for_url("**/login")
        await page.goto(f"{WEB}{project}/executions")
        await page.wait_for_url("**/login**")
        check("redirect=" in page.url, "a signed-out visitor is sent to sign in and back afterwards")

        check(not script_errors, f"no script errors on any page ({script_errors[:2]})")
        await browser.close()

    print(f"\n{len(problems)} problem(s). Screenshots in {OUT}/")
    sys.exit(1 if problems else 0)


asyncio.run(main())
