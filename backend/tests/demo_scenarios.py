"""Ten end-to-end scenarios against the demo application, as executable scripts.

Each one is a script in the platform's own format, with the result it must
give. They are run directly by the engine (`tests/engine/test_demo_scenarios.py`)
and through the whole platform (`tests/integration/test_engine_real.py`).

Three of them are expected **not** to pass: a scenario set in which everything
passes would not show that the engine can tell a wrong application from a right
one.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any


def role(name: str, label: str | None = None, **more: Any) -> dict[str, Any]:
    return {"by": "role", "role": name, **({"name": label} if label else {}), **more}


def label(text: str, **more: Any) -> dict[str, Any]:
    return {"by": "label", "value": text, **more}


def click(name: str) -> dict[str, Any]:
    return {"action": "click", "locator": role("button", name)}


def fill(field_label: str, value: str) -> dict[str, Any]:
    return {"action": "fill", "locator": label(field_label, exact=True), "value": value}


def go(path: str) -> dict[str, Any]:
    return {"action": "navigate", "url": path}


@dataclass(frozen=True, slots=True)
class Scenario:
    key: str
    title: str
    steps: list[dict[str, Any]]
    # The status the case must end with, and for a non-pass the category.
    expected: str = "passed"
    category: str | None = None
    # Execution settings the scenario needs (a narrow window, for instance).
    config: dict[str, Any] = field(default_factory=dict)
    # True if the script has a step that needs a person's approval.
    sensitive: bool = False

    @property
    def script(self) -> dict[str, Any]:
        return {"steps": self.steps}


def scenarios() -> list[Scenario]:
    """A new set each time: names that must be unique in the demo application are random."""
    unique = uuid.uuid4().hex[:8]
    email = f"ada-{unique}@example.org"
    product = f"Oolong {unique}"
    return [
        Scenario(
            "successful_login",
            "Signing in with the right password opens the account page",
            [
                go("/login"),
                fill("Username", "demo"),
                fill("Password", "demo-password"),
                click("Sign in"),
                {"action": "assert_url", "url": {"equals": "/account"}},
                {
                    "action": "assert_text",
                    "locator": {"by": "text", "value": "Signed in as"},
                    "text": {"contains": "Signed in as demo"},
                },
            ],
        ),
        Scenario(
            "failed_login",
            "Signing in with a wrong password is refused with a message",
            [
                go("/login"),
                fill("Username", "demo"),
                fill("Password", "not-the-password"),
                click("Sign in"),
                {
                    "action": "assert_text",
                    "locator": role("alert"),
                    "text": {"equals": "Invalid username or password"},
                },
                {"action": "assert_url", "url": {"equals": "/login"}},
                {"action": "assert_absent", "locator": {"by": "text", "value": "Signed in as"}},
            ],
        ),
        Scenario(
            "customer_registration",
            "A customer with valid details can register",
            [
                go("/register"),
                fill("Full name", "Ada Lovelace"),
                fill("Email", email),
                fill("Password", "a-long-passphrase"),
                fill("Age", "36"),
                click("Create account"),
                {"action": "assert_title", "title": {"equals": "Welcome"}},
                {
                    "action": "assert_text",
                    "locator": role("status"),
                    "text": {"contains": f"Welcome, Ada Lovelace. Your account {email} is ready."},
                },
            ],
        ),
        Scenario(
            "form_validation",
            "Registration lists every problem and keeps what was typed",
            [
                go("/register"),
                fill("Email", "not-an-email"),
                fill("Password", "short"),
                fill("Age", "16"),
                click("Create account"),
                {
                    "action": "assert_text",
                    "locator": role("alert"),
                    "text": {"contains": "Please correct 4 problem(s)"},
                },
                {
                    "action": "assert_count",
                    "locator": {"by": "css", "value": "[role=alert] li"},
                    "count": 4,
                },
                {
                    "action": "assert_visible",
                    "locator": {"by": "text", "value": "Full name is required"},
                },
                {
                    "action": "assert_visible",
                    "locator": {"by": "text", "value": "You must be 18 or older"},
                },
                {
                    "action": "assert_value",
                    "locator": label("Email", exact=True),
                    "value": {"equals": "not-an-email"},
                },
                {"action": "assert_title", "title": {"equals": "Create your account"}},
            ],
        ),
        Scenario(
            "product_creation",
            "A new product appears in the catalogue and in its API",
            [
                go("/products"),
                fill("Product name", product),
                fill("Price", "12.50"),
                fill("Stock", "8"),
                click("Add product"),
                {"action": "assert_visible", "locator": role("cell", product)},
                {"action": "assert_absent", "locator": role("alert")},
                {
                    "action": "api_request",
                    "method": "GET",
                    "url": f"/api/products?name={product.replace(' ', '%20')}",
                    "save_as": "catalogue",
                },
                {"action": "assert_api_status", "response": "catalogue", "status": 200},
                {
                    "action": "assert_api_json",
                    "response": "catalogue",
                    "pointer": "/count",
                    "equals": 1,
                },
                {
                    "action": "assert_api_json",
                    "response": "catalogue",
                    "pointer": "/items/0/name",
                    "equals": product,
                },
                {
                    "action": "assert_api_json",
                    "response": "catalogue",
                    "pointer": "/items/0/price",
                    "equals": 12.5,
                },
            ],
        ),
        Scenario(
            "invalid_input",
            "A product with a negative price and no stock is refused and not listed",
            [
                go("/products"),
                fill("Product name", f"Broken {unique}"),
                fill("Price", "-5"),
                fill("Stock", "many"),
                click("Add product"),
                {
                    "action": "assert_text",
                    "locator": role("alert"),
                    "text": {"contains": "Price must be a number greater than 0"},
                },
                {
                    "action": "assert_visible",
                    "locator": {"by": "text", "value": "Stock must be a whole number, 0 or more"},
                },
                {"action": "assert_absent", "locator": role("cell", f"Broken {unique}")},
            ],
        ),
        Scenario(
            "business_rule",
            "Loyalty points are one per 10 spent, rounded down",
            [
                go("/loyalty"),
                {"action": "fill", "locator": label("Bill amount"), "value": "509"},
                click("Calculate points"),
                {
                    "action": "read_text",
                    "locator": {"by": "test_id", "value": "points"},
                    "save_as": "points",
                    "as_number": True,
                },
                {"action": "assert_expression", "expression": "points == floor(509 / 10)"},
            ],
        ),
        Scenario(
            "business_rule_defect",
            "The same rule on the build with the seeded rounding defect",
            [
                go("/loyalty?variant=rounding"),
                {"action": "fill", "locator": label("Bill amount"), "value": "509"},
                click("Calculate points"),
                {
                    "action": "read_text",
                    "locator": {"by": "test_id", "value": "points"},
                    "save_as": "points",
                    "as_number": True,
                },
                {"action": "assert_expression", "expression": "points == floor(509 / 10)"},
            ],
            expected="failed",
            category="test_assertion_failure",
        ),
        Scenario(
            "api_response_validation",
            "The points API answers with the documented shape and value",
            [
                {
                    "action": "api_request",
                    "method": "GET",
                    "url": "/api/points?amount=250",
                    "save_as": "points",
                },
                {"action": "assert_api_status", "response": "points", "status": 200},
                {
                    "action": "assert_api_json",
                    "response": "points",
                    "pointer": "/points",
                    "equals": 25,
                },
                {
                    "action": "assert_api_json",
                    "response": "points",
                    "pointer": "/points",
                    "type": "number",
                },
                {
                    "action": "api_request",
                    "method": "GET",
                    "url": "/api/broken",
                    "save_as": "broken",
                },
                {"action": "assert_api_status", "response": "broken", "status": 200},
            ],
            expected="failed",
            category="application_failure",
        ),
        Scenario(
            "responsive_layout",
            "On a narrow window the menu button replaces the navigation",
            [
                go("/responsive"),
                {"action": "assert_visible", "locator": role("button", "Menu")},
                {"action": "assert_hidden", "locator": {"by": "css", "value": "nav.full-nav"}},
                go("/products"),
                {"action": "assert_visible", "locator": role("button", "Add product")},
            ],
            config={"viewport": {"width": 400, "height": 800}},
        ),
        Scenario(
            "responsive_layout_wide",
            "On a wide window the navigation is shown and the menu button is not",
            [
                go("/responsive"),
                {"action": "assert_hidden", "locator": role("button", "Menu")},
                {"action": "assert_visible", "locator": role("link", "Products")},
            ],
        ),
        Scenario(
            "browser_navigation",
            "Links lead from the home page to other pages and back",
            [
                go("/"),
                {"action": "assert_title", "title": {"equals": "Demo shop"}},
                {"action": "click", "locator": role("link", "Loyalty points")},
                {"action": "assert_url", "url": {"equals": "/loyalty"}},
                {"action": "assert_title", "title": {"equals": "Loyalty points"}},
                go("/register"),
                {"action": "click", "locator": role("link", "Back to the shop")},
                {"action": "assert_url", "url": {"equals": "/"}},
                {"action": "click", "locator": role("link", "A page that does not exist")},
            ],
            expected="error",
            category="test_automation_failure",
        ),
    ]
