"""Workflow step: approved plan -> test cases.

One model call covers a few objectives at a time, so answers stay small enough
to be complete. A standalone function for now; Phase 8 places it in the
LangGraph workflow unchanged.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from app.agent.guard import Fence, clean_text
from app.agent.llm.base import LLMGateway, LLMRequest, LLMResult, Recorder
from app.agent.prompts import Prompt, get_prompt
from app.testing.cases import CaseContent, GeneratedCases

PROMPT_NAME = "test_case_generation"


@dataclass(frozen=True, slots=True)
class GenerationContext:
    """Names and settings only. There is no field through which a secret could arrive."""

    application: dict[str, Any]
    environment: dict[str, Any]
    available_credential_roles: list[str]
    configured_rules: list[dict[str, str]] = field(default_factory=list)
    requested_test_types: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RewriteRequest:
    cases: list[CaseContent]
    feedback: str | None


def build_request(
    *,
    plan_scope: dict[str, Any],
    objectives_to_cover: list[str],
    context: GenerationContext,
    existing_titles: list[str],
    max_cases: int,
    rewrite: RewriteRequest | None = None,
    prompt: Prompt | None = None,
    recorder: Recorder | None = None,
) -> LLMRequest[GeneratedCases]:
    prompt = prompt or get_prompt(PROMPT_NAME)
    fence = Fence.new()
    context_json = json.dumps(
        {
            "application": context.application,
            "environment": context.environment,
            "available_credential_roles": context.available_credential_roles,
            "configured_rules": context.configured_rules,
            "requested_test_types": context.requested_test_types,
        },
        ensure_ascii=False,
        indent=2,
    )
    plan_json = json.dumps(
        {
            "plan": plan_scope,
            "objectives_to_cover": objectives_to_cover,
            "existing_case_titles": existing_titles,
            "max_cases": max_cases,
        },
        ensure_ascii=False,
        indent=2,
    )
    review_json = "(none)"
    if rewrite is not None:
        review_json = json.dumps(
            {
                "cases_to_rewrite": [case.model_dump(mode="json") for case in rewrite.cases],
                "reviewer_feedback": clean_text(rewrite.feedback or "", max_chars=2000) or None,
            },
            ensure_ascii=False,
            indent=2,
        )
    return LLMRequest(
        purpose="generation",
        system=prompt.system,
        user=prompt.render_user(
            context=fence.wrap("context", context_json),
            plan=fence.wrap("plan", plan_json),
            review=fence.wrap("review", review_json),
        ),
        schema=GeneratedCases,
        prompt=prompt.ref,
        recorder=recorder,
    )


async def generate_test_cases(
    gateway: LLMGateway,
    *,
    plan_scope: dict[str, Any],
    objectives_to_cover: list[str],
    context: GenerationContext,
    existing_titles: list[str],
    max_cases: int,
    rewrite: RewriteRequest | None = None,
    recorder: Recorder | None = None,
) -> LLMResult[GeneratedCases]:
    """Raises `LLMError` if no valid answer could be obtained."""
    return await gateway.structured(
        build_request(
            plan_scope=plan_scope,
            objectives_to_cover=objectives_to_cover,
            context=context,
            existing_titles=existing_titles,
            max_cases=max_cases,
            rewrite=rewrite,
            recorder=recorder,
        )
    )
