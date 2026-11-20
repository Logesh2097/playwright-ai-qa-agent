"""Fill a development stack with something to look at.

Creates, through the public API and nothing else:

- an account (DEMO_EMAIL / DEMO_PASSWORD) owning the organization "Demo";
- a project "Demo shop" whose application is the bundled demo application;
- an environment with a test login for it;
- three approved test cases in an approved suite. One of them is expected to
  fail: it runs against a page with a seeded defect;
- one execution of that suite, so there is a report with evidence.

Run with `make demo`. Safe to run again: it stops if the project already exists.
Development only. It refuses to run against anything but APP_ENV=development.
"""

import asyncio
import os
import sys
from typing import Any

import httpx

API = os.environ.get("DEMO_API_URL", "http://localhost:8000") + "/api/v1"
DEMO = "http://demo-app:8080"
EMAIL = os.environ.get("DEMO_EMAIL", "demo@example.com")
PASSWORD = os.environ.get("DEMO_PASSWORD", "")
PROJECT = "Demo shop"

LOGIN_RECIPE = [
    {"action": "goto", "path": "/login"},
    {"action": "fill", "target": {"role": "textbox", "name": "Username"}, "value": "{{cred.username}}"},
    {"action": "fill", "target": {"role": "textbox", "name": "Password"}, "value": "{{cred.password}}"},
    {"action": "click", "target": {"role": "button", "name": "Sign in"}},
    {"action": "expect", "text": "Signed in as", "url_path": "/account"},
]  # fmt: skip


def points_case(title: str, amount: str) -> dict[str, Any]:
    return {
        "title": title, "description": "Loyalty points on the demo shop.", "category": "business_logic",
        "test_type": "ui", "objective_id": None, "preconditions": [],
        "test_data": [{"name": "bill_amount", "value": amount, "description": None}],
        "steps": [
            {"action": "navigate", "description": "Open the loyalty page", "target": None, "value": "/loyalty", "side_effect": "none"},
            {"action": "fill", "description": "Enter the bill amount", "target": "the Bill amount field", "value": "{{data.bill_amount}}", "side_effect": "none"},
            {"action": "click", "description": "Calculate the points", "target": "the Calculate points button", "value": None, "side_effect": "none"},
            {"action": "assert_text", "description": "Check the points", "target": "the points earned", "value": str(int(amount) // 10), "side_effect": "none"},
        ],
        "expected_result": f"{int(amount) // 10} points are shown", "expected_source": "human_specified",
        "priority": "p1", "severity": "major", "tags": ["demo"], "dependencies": [],
        "automation_feasibility": "automatable", "automation_notes": None,
        "verification": {"method": "ui_assertion", "description": "One point per 10 spent, rounded down"},
        "assumptions": [],
    }  # fmt: skip


def points_script(path: str) -> dict[str, Any]:
    return {"steps": [
        {"action": "navigate", "url": path},
        {"action": "fill", "locator": {"by": "label", "value": "Bill amount"}, "value": "{{data.bill_amount}}"},
        {"action": "click", "locator": {"by": "role", "role": "button", "name": "Calculate points"}},
        {"action": "read_text", "locator": {"by": "test_id", "value": "points"}, "save_as": "points", "as_number": True},
        {"action": "assert_expression", "description": "One point for every 10 spent, rounded down",
         "expression": "points == floor(number(data.bill_amount) / 10)"},
    ]}  # fmt: skip


CASES = [
    ("Loyalty points for a bill of 500", "/loyalty", "500"),
    ("Loyalty points round down (bill of 509)", "/loyalty", "509"),
    ("Loyalty points on the build with a rounding defect", "/loyalty?variant=rounding", "506"),
]


async def session(client: httpx.AsyncClient) -> dict[str, Any]:
    login = await client.post(f"{API}/auth/login", json={"email": EMAIL, "password": PASSWORD})
    if login.status_code == 200:
        return login.json()  # type: ignore[no-any-return]
    created = await client.post(
        f"{API}/auth/register",
        json={"email": EMAIL, "password": PASSWORD, "full_name": "Demo User", "organization_name": "Demo"},
    )  # fmt: skip
    if created.status_code != 201:
        sys.exit(
            f"Could not sign in or register {EMAIL} ({login.status_code}, {created.status_code}). "
            "If the account exists with another password, set DEMO_PASSWORD in .env to it."
        )
    return created.json()  # type: ignore[no-any-return]


async def main() -> None:
    if os.environ.get("APP_ENV", "development") != "development":
        sys.exit("Demo data is for development stacks only.")
    if len(PASSWORD) < 12:
        sys.exit("Set DEMO_PASSWORD (12+ characters). `make demo` generates one for you.")

    async with httpx.AsyncClient(timeout=60) as client:
        signed_in = await session(client)
        org = signed_in["memberships"][0]["organization_id"]
        client.headers["Authorization"] = f"Bearer {signed_in['access_token']}"

        async def post(url: str, **body: Any) -> Any:
            response = await client.post(url, json=body or None)
            if response.status_code >= 300:
                sys.exit(f"{url} answered {response.status_code}: {response.text[:300]}")
            return response.json()

        base = f"{API}/organizations/{org}/projects"
        existing = (await client.get(base)).json()
        items = existing["items"] if isinstance(existing, dict) else existing
        if any(p["name"] == PROJECT for p in items):
            print(f"The project '{PROJECT}' already exists; nothing was changed.")
            return

        project = await post(base, name=PROJECT)
        b = f"{base}/{project['id']}"
        app = await post(f"{b}/applications", name="Demo app", base_url=DEMO)
        env = await post(
            f"{b}/applications/{app['id']}/environments",
            name="demo", kind="development", base_url=DEMO,
            allow_private_network=True, is_read_only=False, requires_approval=False,
        )  # fmt: skip
        await post(
            f"{b}/applications/{app['id']}/environments/{env['id']}/credentials",
            name="customer", role_label="customer", kind="username_password",
            # The demo application's own fixture login, not a secret.
            secret={"username": "demo", "password": "demo-password"},
            is_test_account=True, login_recipe=LOGIN_RECIPE,
        )  # fmt: skip

        case_ids = []
        for title, path, amount in CASES:
            case = await post(
                f"{b}/test-cases", application_id=app["id"], environment_id=env["id"],
                content=points_case(title, amount),
            )  # fmt: skip
            saved = await client.put(f"{b}/test-cases/{case['id']}/automation", json=points_script(path))
            if saved.status_code != 200:
                sys.exit(f"Script for '{title}' was refused: {saved.text[:300]}")
            await post(f"{b}/test-cases/{case['id']}/approve")
            case_ids.append(case["id"])
        suite = await post(
            f"{b}/test-suites", name="Loyalty points", application_id=app["id"], case_ids=case_ids
        )
        await post(f"{b}/test-suites/{suite['id']}/approve")

        run = await post(f"{b}/executions", environment_id=env["id"], suite_id=suite["id"])
        verdict = "still running"
        for _ in range(90):
            state = (await client.get(f"{b}/executions/{run['id']}")).json()
            if state.get("finished_at"):
                verdict = state.get("verdict") or state.get("status") or "finished"
                break
            await asyncio.sleep(2)

        print(f"Project '{PROJECT}': 1 application, 1 environment, 1 test login, 3 approved cases, 1 suite.")
        print(f"First run of the suite: {verdict} (one case is meant to fail).")
        print(f"PROJECT_PATH=/projects/{project['id']}")
        print(f"REPORT_PATH=/projects/{project['id']}/executions/{run['id']}")


asyncio.run(main())
