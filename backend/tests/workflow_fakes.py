"""A scripted stand-in for the AI model, for workflow tests.

It answers each kind of request the workflow makes (requirement analysis, test
cases, scripts, failure explanations) with valid output by default. Tests queue
something else where the point of the test is how the workflow copes with it.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.agent.llm.base import LLMCallRecord, LLMError, LLMRequest, LLMResult, LLMUsage
from app.agent.schemas.failure import FailureExplanations
from app.agent.schemas.requirement import RequirementAnalysis
from app.agent.schemas.script import GeneratedScripts
from app.testing.cases import GeneratedCases
from tests.factories import analysis_dict, generated

_STEP_FIELDS = (
    "description", "locator", "within", "url", "value", "label", "comparison", "expected", "state",
    "save_as", "as_number", "attribute", "file", "method", "json_body", "response", "status",
    "pointer", "json_equals", "exists", "count", "checked", "expression",
)  # fmt: skip


def step(action: str, **fields: Any) -> dict[str, Any]:
    """One step in the form the model fills: every field present, unused ones null."""
    return {"action": action, **dict.fromkeys(_STEP_FIELDS), **fields}


def by_role(role: str, name: str) -> dict[str, Any]:
    return {"by": "role", "role": role, "name": name, "value": None, "exact": False}


def by_label(value: str) -> dict[str, Any]:
    return {"by": "label", "role": None, "name": None, "value": value, "exact": False}


def login_script(key: str, **overrides: Any) -> dict[str, Any]:
    """Types into the login form and checks the result: it changes state."""
    return {
        "case_key": key,
        "login_as": None,
        "steps": [
            step("navigate", url="/login"),
            step("fill", locator=by_label("Username"), value="{{cred.customer.username}}"),
            step("fill", locator=by_label("Password"), value="{{cred.customer.password}}"),
            step("click", locator=by_role("button", "Sign in")),
            step("assert_url", comparison="equals", expected="/account"),
        ],
        "cleanup": [],
        "unresolved": [],
        **overrides,
    }


def look_script(key: str, **overrides: Any) -> dict[str, Any]:
    """Only opens a page and checks it: it changes nothing."""
    return {
        "case_key": key,
        "login_as": None,
        "steps": [
            step("navigate", url="/login"),
            step("assert_title", comparison="equals", expected="Sign in"),
        ],
        "cleanup": [],
        "unresolved": [],
        **overrides,
    }


def two_cases() -> dict[str, Any]:
    return {
        "cases": [
            generated("C1"),
            generated(
                "C2",
                title="Invalid password is rejected",
                description="A wrong password does not sign the customer in.",
                category="negative",
                objective_id="OBJ-2",
                expected_result="An error is shown and the customer stays signed out.",
                tags=["login"],
                verification={
                    "method": "ui_assertion",
                    "description": "An error message is visible.",
                },
                steps=[
                    {
                        "action": "navigate",
                        "description": "Open the login page",
                        "target": None,
                        "value": "/login",
                        "side_effect": "none",
                    },
                    {
                        "action": "fill",
                        "description": "Enter the customer's email",
                        "target": "the Email field",
                        "value": "{{cred.customer.username}}",
                        "side_effect": "none",
                    },
                    {
                        "action": "fill",
                        "description": "Enter a wrong password",
                        "target": "the Password field",
                        "value": "not-the-right-one",
                        "side_effect": "none",
                    },
                    {
                        "action": "click",
                        "description": "Submit the form",
                        "target": "the Sign in button",
                        "value": None,
                        "side_effect": "none",
                    },
                    {
                        "action": "assert_visible",
                        "description": "An error is shown",
                        "target": "the error message",
                        "value": None,
                        "side_effect": "none",
                    },
                ],
            ),
        ]
    }


@dataclass
class WorkflowModel:
    """Implements the gateway. Queues hold answers (or exceptions) for the next requests of a kind."""

    configured: bool = True
    analyses: list[Any] = field(default_factory=list)
    cases: list[Any] = field(default_factory=list)
    # Each item: an exception, a full answer, or a function from case key to a script.
    scripts: list[Any] = field(default_factory=list)
    explanations: list[Any] = field(default_factory=list)
    default_script: Any = login_script
    requests: list[LLMRequest[Any]] = field(default_factory=list)

    def of(self, schema: type) -> list[LLMRequest[Any]]:
        return [request for request in self.requests if request.schema is schema]

    def _answer(self, request: LLMRequest[Any]) -> Any:
        if request.schema is RequirementAnalysis:
            return self.analyses.pop(0) if self.analyses else analysis_dict()
        if request.schema is GeneratedCases:
            return self.cases.pop(0) if self.cases else two_cases()
        if request.schema is GeneratedScripts:
            queued = self.scripts.pop(0) if self.scripts else self.default_script
            if isinstance(queued, Exception | dict):
                return queued
            keys = list(dict.fromkeys(re.findall(r'"case_key": "([^"]+)"', request.user)))
            return {"scripts": [queued(key) for key in keys]}
        if request.schema is FailureExplanations:
            if self.explanations:
                return self.explanations.pop(0)
            refs = list(dict.fromkeys(re.findall(r'"ref": "(R\d+)"', request.user)))
            return {
                "explanations": [
                    {
                        "ref": ref,
                        "likely_cause": "application_defect",
                        "summary": "The page showed a different value than the case expects.",
                        "supporting_facts": ["expected 50, found 51"],
                        "possible_root_cause": "Possibly the points are rounded differently.",
                        "impact": "Customers could be shown the wrong number of points.",
                        "debugging_steps": ["Check the calculation."],
                        "suggested_test_cases": [
                            {
                                "title": "Points for an amount ending in .50",
                                "why": "Shows rounding.",
                            }
                        ],
                        "confidence": "medium",
                    }
                    for ref in refs
                ]
            }
        raise AssertionError(f"unexpected request for {request.schema.__name__}")

    async def structured(self, request: LLMRequest[Any]) -> LLMResult[Any]:
        self.requests.append(request)
        answer = self._answer(request)
        usage = LLMUsage(input_tokens=1000, output_tokens=200, cost_usd=Decimal("0.0100"))
        if request.recorder is not None:
            await request.recorder(
                LLMCallRecord(
                    purpose=request.purpose,
                    model="fake-model",
                    prompt=request.prompt,
                    status=answer.code if isinstance(answer, LLMError) else "ok",
                    usage=LLMUsage() if isinstance(answer, Exception) else usage,
                    latency_ms=5,
                    attempt=1,
                )
            )
        if isinstance(answer, Exception):
            raise answer
        return LLMResult(
            output=request.schema.model_validate(answer),
            model="fake-model",
            usage=usage,
            calls=1,
            latency_ms=5,
        )
