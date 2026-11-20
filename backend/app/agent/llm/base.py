"""Provider-independent contract for structured LLM calls."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    # None when no price is configured for the model.
    cost_usd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class LLMCallRecord:
    """One HTTP call to the provider, successful or not. Written to the usage log."""

    purpose: str
    model: str
    prompt: str  # "<name>@<version>"
    status: str  # "ok" or an error code
    usage: LLMUsage
    latency_ms: int
    attempt: int


Recorder = Callable[[LLMCallRecord], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class LLMRequest[T: BaseModel]:
    """A request for output that must validate against `schema`."""

    purpose: str  # which model role to use, e.g. "analysis"
    system: str
    user: str
    schema: type[T]
    prompt: str = "unnamed@0"
    recorder: Recorder | None = None


@dataclass(frozen=True, slots=True)
class LLMResult[T: BaseModel]:
    output: T
    model: str
    usage: LLMUsage
    calls: int
    latency_ms: int


class LLMError(Exception):
    """A structured LLM call failed. `code` is stable and safe to show to users."""

    code = "llm_error"
    retryable = False
    message = "The AI service could not complete the request."

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.message)


class LLMNotConfiguredError(LLMError):
    code = "llm_not_configured"
    message = "The AI service is not configured on this installation."


class LLMTimeoutError(LLMError):
    code = "llm_timeout"
    retryable = True
    message = "The AI service did not answer in time."


class LLMUnavailableError(LLMError):
    code = "llm_unavailable"
    retryable = True
    message = "The AI service is temporarily unavailable."


class LLMRateLimitedError(LLMError):
    code = "llm_rate_limited"
    retryable = True
    message = "The AI service is rate limiting requests. Please try again shortly."

    def __init__(self, detail: str | None = None, *, retry_after_s: float | None = None) -> None:
        super().__init__(detail)
        self.retry_after_s = retry_after_s


class LLMRejectedError(LLMError):
    """The provider refused the request itself: bad key, unknown model, invalid request."""

    code = "llm_request_rejected"
    message = (
        "The AI service rejected the request. An administrator should check its configuration."
    )


class LLMInvalidOutputError(LLMError):
    code = "llm_invalid_output"
    message = "The AI service returned a response that did not match the required structure."


class LLMRefusalError(LLMError):
    code = "llm_refusal"
    message = "The AI service declined to process this instruction."


class LLMOutputTruncatedError(LLMError):
    code = "llm_output_truncated"
    message = "The AI response was cut off before it was complete."


class LLMGateway(Protocol):
    @property
    def configured(self) -> bool: ...

    async def structured[T: BaseModel](self, request: LLMRequest[T]) -> LLMResult[T]:
        """Return output validated against `request.schema`, or raise an `LLMError`."""
        ...
