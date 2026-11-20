"""Ask the model to explain results that did not pass. The answer is advice only."""

import json
from typing import Any

from app.agent.guard import Fence
from app.agent.llm.base import LLMGateway, LLMRequest, LLMResult, Recorder
from app.agent.prompts import Prompt, get_prompt
from app.agent.schemas.failure import FailureExplanations

PROMPT_NAME = "failure_analysis"


def build_request(
    failures: list[dict[str, Any]],
    *,
    prompt: Prompt | None = None,
    recorder: Recorder | None = None,
) -> LLMRequest[FailureExplanations]:
    prompt = prompt or get_prompt(PROMPT_NAME)
    return LLMRequest(
        purpose="analysis",
        system=prompt.system,
        user=prompt.render_user(
            failures=Fence.new().wrap(
                "failures", json.dumps(failures, ensure_ascii=False, indent=2)
            )
        ),
        schema=FailureExplanations,
        prompt=prompt.ref,
        recorder=recorder,
    )


async def explain_failures(
    gateway: LLMGateway, failures: list[dict[str, Any]], *, recorder: Recorder | None = None
) -> LLMResult[FailureExplanations]:
    """Raises `LLMError` if no valid answer could be obtained."""
    return await gateway.structured(build_request(failures, recorder=recorder))
