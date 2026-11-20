"""Workflow step: testing instruction -> structured requirement.

A standalone function for now; Phase 8 places it in the LangGraph workflow
unchanged.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from app.agent.guard import Fence, clean_text
from app.agent.llm.base import LLMGateway, LLMRequest, LLMResult, Recorder
from app.agent.prompts import Prompt, get_prompt
from app.agent.schemas.requirement import RequirementAnalysis

PROMPT_NAME = "requirement_analysis"


@dataclass(frozen=True, slots=True)
class AnalysisContext:
    """Facts about the target, read from the platform's own database.

    Contains names and settings only. Credential secrets are not part of this
    type, so they cannot reach a prompt through it.
    """

    application: dict[str, Any]
    environment: dict[str, Any]
    available_credential_roles: list[str]
    variable_names: list[str]
    configured_rules: list[dict[str, str]] = field(default_factory=list)
    requested_test_types: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Clarification:
    question: str
    answer: str


def build_request(
    *,
    instruction: str,
    context: AnalysisContext,
    clarifications: list[Clarification],
    max_chars: int,
    prompt: Prompt | None = None,
    recorder: Recorder | None = None,
) -> LLMRequest[RequirementAnalysis]:
    prompt = prompt or get_prompt(PROMPT_NAME)
    fence = Fence.new()
    context_json = json.dumps(
        {
            "application": context.application,
            "environment": context.environment,
            "available_credential_roles": context.available_credential_roles,
            "variable_names": context.variable_names,
            "configured_rules": context.configured_rules,
            "requested_test_types": context.requested_test_types,
        },
        ensure_ascii=False,
        indent=2,
    )
    answers = (
        "\n".join(
            f"Q: {clean_text(item.question, max_chars=500)}\nA: {clean_text(item.answer, max_chars=1000)}"
            for item in clarifications
        )
        or "(none)"
    )
    return LLMRequest(
        purpose="analysis",
        system=prompt.system,
        # The system message is fixed text. Everything that varies goes into the user
        # message, inside fences.
        user=prompt.render_user(
            context=fence.wrap("context", context_json),
            instruction=fence.wrap("instruction", clean_text(instruction, max_chars=max_chars)),
            clarifications=fence.wrap("clarifications", answers),
        ),
        schema=RequirementAnalysis,
        prompt=prompt.ref,
        recorder=recorder,
    )


async def analyse_requirement(
    gateway: LLMGateway,
    *,
    instruction: str,
    context: AnalysisContext,
    clarifications: list[Clarification],
    max_chars: int,
    recorder: Recorder | None = None,
) -> LLMResult[RequirementAnalysis]:
    """Raises `LLMError` if no valid analysis could be obtained."""
    return await gateway.structured(
        build_request(
            instruction=instruction,
            context=context,
            clarifications=clarifications,
            max_chars=max_chars,
            recorder=recorder,
        )
    )
