"""Builders for test data."""

import json
from typing import Any

import httpx

from app.agent.schemas.requirement import RequirementAnalysis

LOGIN_INSTRUCTION = (
    "Test the customer login functionality. Verify valid login, invalid password, "
    "empty email, empty password and incorrect credentials."
)
LOYALTY_INSTRUCTION = (
    "Test loyalty points calculation with bill amounts below, equal to and above "
    "the configured minimum bill amount."
)


def analysis_dict(**overrides: Any) -> dict[str, Any]:
    """A complete, valid analysis of the login instruction, with every outcome stated."""
    data: dict[str, Any] = {
        "summary": "Verify that customers can sign in and that invalid attempts are rejected.",
        "feature_name": "Customer login",
        "is_feature_identifiable": True,
        "application_context": {
            "application": "Storefront",
            "module": "Authentication",
            "user_actions": ["submit the login form"],
        },
        "preconditions": ["A customer account exists"],
        "test_objectives": [
            {"id": "OBJ-1", "title": "Valid login succeeds", "category": "positive"},
            {"id": "OBJ-2", "title": "Invalid password is rejected", "category": "negative"},
            {"id": "OBJ-3", "title": "Empty email is rejected", "category": "validation"},
        ],
        "expected_behavior": [
            {
                "objective_id": "OBJ-1",
                "description": "The customer is signed in and sees their account page.",
                "source": "clarification_answer",
            },
            {
                "objective_id": "OBJ-2",
                "description": "An error is shown and the customer stays signed out.",
                "source": "clarification_answer",
            },
            {
                "objective_id": "OBJ-3",
                "description": "The form reports that the email is required.",
                "source": "clarification_answer",
            },
        ],
        "business_rules": [],
        "validation_requirements": ["Email is required", "Password is required"],
        "negative_scenarios": ["Wrong password", "Unknown email"],
        "edge_cases": [],
        "required_test_data": [
            {
                "name": "Customer account",
                "kind": "credential",
                "description": "A valid customer login",
                "credential_role": "customer",
                "values": [],
                "source": "provided_configuration",
            }
        ],
        "dependencies": [],
        "constraints": [],
        "assumptions": [],
        "missing_information": [],
        "clarification_questions": [],
        "requires_data_changes": False,
        "mentioned_targets": [],
        "ignored_instructions": [],
    }
    data.update(overrides)
    return data


def analysis(**overrides: Any) -> RequirementAnalysis:
    return RequirementAnalysis.model_validate(analysis_dict(**overrides))


def loyalty_analysis_dict(*, minimum_known: bool) -> dict[str, Any]:
    """The loyalty instruction: the outcome depends on a minimum nobody may have supplied."""
    source = "clarification_answer" if minimum_known else "unknown"
    return analysis_dict(
        summary="Verify loyalty points around the minimum bill amount.",
        feature_name="Loyalty points calculation",
        application_context={
            "application": "Storefront",
            "module": "Loyalty",
            "user_actions": ["complete a purchase"],
        },
        test_objectives=[
            {"id": "OBJ-1", "title": "Bill below the minimum", "category": "boundary"},
            {"id": "OBJ-2", "title": "Bill equal to the minimum", "category": "boundary"},
            {"id": "OBJ-3", "title": "Bill above the minimum", "category": "boundary"},
        ],
        expected_behavior=[
            {
                "objective_id": f"OBJ-{n}",
                "description": text if minimum_known else None,
                "source": source,
            }
            for n, text in (
                (1, "No points are awarded."),
                (2, "Points are awarded: one per 100 of the bill."),
                (3, "Points are awarded: one per 100 of the bill."),
            )
        ],
        business_rules=[
            {
                "name": "Minimum bill amount",
                "description": "Points are only awarded from this bill amount upwards.",
                "source": source,
                "value_known": minimum_known,
            }
        ],
        required_test_data=[],
        missing_information=[]
        if minimum_known
        else [
            {
                "topic": "Minimum bill amount",
                "why_needed": "The boundary values depend on it.",
                "blocking": True,
            }
        ],
        clarification_questions=[]
        if minimum_known
        else [
            {
                "id": "Q-1",
                "question": "What is the configured minimum bill amount, and how are points calculated?",
                "options": [],
                "related_to": "Minimum bill amount",
            }
        ],
        requires_data_changes=True,
    )


def completion(
    content: str | None,
    *,
    finish: str = "stop",
    refusal: str | None = None,
    prompt_tokens: int = 1200,
    completion_tokens: int = 300,
) -> dict[str, Any]:
    """A chat-completions response body in the provider's wire format."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def ok(payload: dict[str, Any] | None = None, **kwargs: Any) -> httpx.Response:
    return httpx.Response(200, json=completion(json.dumps(payload or analysis_dict()), **kwargs))


def api_error(status: int, message: str = "provider detail", **headers: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": message}}, headers=headers)


# --- test cases ------------------------------------------------------------------------


def case_dict(**overrides: Any) -> dict[str, Any]:
    """A complete, valid test case for the first objective of the login plan."""
    data: dict[str, Any] = {
        "title": "Valid login succeeds",
        "description": "A customer with correct credentials is signed in.",
        "category": "positive",
        "test_type": "ui",
        "objective_id": "OBJ-1",
        "preconditions": ["A customer account exists"],
        "test_data": [],
        "steps": [
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
                "description": "Enter the customer's password",
                "target": "the Password field",
                "value": "{{cred.customer.password}}",
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
                "description": "The account page is shown",
                "target": "the My account heading",
                "value": None,
                "side_effect": "none",
            },
        ],
        "expected_result": "The customer is signed in and sees their account page.",
        "expected_source": "plan_objective",
        "priority": "p0",
        "severity": "critical",
        "tags": ["login", "smoke"],
        "dependencies": [],
        "automation_feasibility": "automatable",
        "automation_notes": None,
        "verification": {
            "method": "ui_assertion",
            "description": "The My account heading is visible after submitting.",
        },
        "assumptions": [],
    }
    data.update(overrides)
    return data


def generated(local_id: str = "C1", **overrides: Any) -> dict[str, Any]:
    return {"local_id": local_id, **case_dict(**overrides)}


def _bill_steps(expected_points: str | None) -> list[dict[str, Any]]:
    return [
        {
            "action": "navigate",
            "description": "Open the billing page",
            "target": None,
            "value": "/billing",
            "side_effect": "none",
        },
        {
            "action": "fill",
            "description": "Enter the bill amount",
            "target": "the Bill amount field",
            "value": "{{data.bill_amount}}",
            "side_effect": "none",
        },
        {
            "action": "click",
            "description": "Calculate the points",
            "target": "the Calculate button",
            "value": None,
            "side_effect": "none",
        },
        {
            "action": "assert_text",
            "description": "Check the points shown",
            "target": "the Loyalty points value",
            "value": expected_points or "TO BE DEFINED",
            "side_effect": "none",
        },
    ]


def loyalty_cases() -> list[dict[str, Any]]:
    """What a careful model returns for the loyalty plan with a minimum of 500.

    The three boundary cases take their result from the plan. Everything beyond the
    plan has no stated result, so it is left unknown rather than guessed.
    """

    def bill(local_id: str, title: str, amount: str | None, **extra: Any) -> dict[str, Any]:
        known = extra.pop("known", None)
        return generated(
            local_id,
            title=title,
            description=f"Loyalty points for: {title.lower()}.",
            category=extra.pop("category", "boundary"),
            objective_id=extra.pop("objective_id", None),
            preconditions=["A customer is selected"],
            test_data=[{"name": "bill_amount", "value": amount, "description": "Bill total"}],
            steps=_bill_steps(extra.pop("points", None)),
            expected_result=known,
            expected_source="plan_objective" if known else "unknown",
            priority="p1",
            severity="major",
            tags=["loyalty"],
            verification={
                "method": "ui_assertion",
                "description": "The points value matches the expected number.",
            },
            **extra,
        )

    return [
        bill(
            "C1",
            "Bill amount below the minimum",
            "499",
            objective_id="OBJ-1",
            known="No points are awarded.",
            points="0",
        ),
        bill(
            "C2",
            "Bill amount equal to the minimum",
            "500",
            objective_id="OBJ-2",
            known="Points are awarded: one per 100 of the bill.",
            points="5",
        ),
        bill(
            "C3",
            "Bill amount above the minimum",
            "501",
            objective_id="OBJ-3",
            known="Points are awarded: one per 100 of the bill.",
            points="5",
        ),
        bill("C4", "Empty bill amount", "", category="form_validation"),
        bill("C5", "Invalid bill amount", "abc", category="form_validation"),
        bill("C6", "Zero bill amount", "0", category="edge_case"),
        bill("C7", "Negative bill amount", "-100", category="edge_case"),
        bill("C8", "Decimal bill amount", "500.50", category="edge_case"),
        bill("C9", "Missing customer details", "600", category="negative"),
        bill("C10", "Invalid loyalty configuration", "700", category="business_logic"),
    ]
