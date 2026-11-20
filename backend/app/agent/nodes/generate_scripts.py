"""Ask the model to turn reviewed case descriptions into engine scripts."""

import json
from dataclasses import dataclass, field
from typing import Any

from app.agent.guard import Fence
from app.agent.llm.base import LLMGateway, LLMRequest, LLMResult, Recorder
from app.agent.prompts import Prompt, get_prompt
from app.agent.schemas.script import GeneratedScripts
from app.testing.cases import CaseContent

PROMPT_NAME = "script_generation"


@dataclass(frozen=True, slots=True)
class ScriptContext:
    """Names and settings only. There is no field through which a secret could arrive."""

    base_url: str
    read_only: bool
    allowed_actions: list[str]
    available_credential_roles: list[str]
    environment_variables: list[str]
    available_files: list[str]


@dataclass(frozen=True, slots=True)
class CaseToScript:
    key: str
    content: CaseContent
    # Problems of the script written earlier, when asking again.
    problems: list[str] = field(default_factory=list)


def build_request(
    *,
    context: ScriptContext,
    pages: list[dict[str, Any]],
    cases: list[CaseToScript],
    prompt: Prompt | None = None,
    recorder: Recorder | None = None,
) -> LLMRequest[GeneratedScripts]:
    prompt = prompt or get_prompt(PROMPT_NAME)
    fence = Fence.new()
    context_json = json.dumps(
        {
            "base_url": context.base_url,
            "read_only": context.read_only,
            "allowed_actions": context.allowed_actions,
            "available_credential_roles": context.available_credential_roles,
            "environment_variables": context.environment_variables,
            "available_files": context.available_files,
        },
        ensure_ascii=False,
        indent=2,
    )
    cases_json = json.dumps(
        {
            "cases": [
                {
                    "case_key": case.key,
                    "case": case.content.model_dump(mode="json"),
                    "problems_in_previous_script": case.problems,
                }
                for case in cases
            ]
        },
        ensure_ascii=False,
        indent=2,
    )
    return LLMRequest(
        purpose="generation",
        system=prompt.system,
        user=prompt.render_user(
            context=fence.wrap("context", context_json),
            # Page titles and element names were written by the application under test.
            pages=fence.wrap("pages", json.dumps(pages, ensure_ascii=False, indent=2)),
            cases=fence.wrap("cases", cases_json),
        ),
        schema=GeneratedScripts,
        prompt=prompt.ref,
        recorder=recorder,
    )


async def generate_scripts(
    gateway: LLMGateway,
    *,
    context: ScriptContext,
    pages: list[dict[str, Any]],
    cases: list[CaseToScript],
    recorder: Recorder | None = None,
) -> LLMResult[GeneratedScripts]:
    """Raises `LLMError` if no valid answer could be obtained."""
    return await gateway.structured(
        build_request(context=context, pages=pages, cases=cases, recorder=recorder)
    )
