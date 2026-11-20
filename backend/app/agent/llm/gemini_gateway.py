"""Gemini implementation of the gateway, through Google's native API.

Everything the gateway promises (validated output, one repair, counted retries,
error messages without provider text, a record of every call) is inherited. This
class only says how to build the client, how to ask for structured output and how
to read Google's errors.

Gemini's OpenAI-compatible endpoint is deliberately not used: it loses parts of
multi-step answers that the native API keeps.
"""

import re
from typing import Any

import httpx
from google.genai import errors as google_errors
from langchain_core.exceptions import ContextOverflowError
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel

from app.agent.llm.base import (
    LLMError,
    LLMOutputTruncatedError,
    LLMRateLimitedError,
    LLMRefusalError,
    LLMRejectedError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.agent.llm.config import ModelConfig
from app.agent.llm.openai_gateway import OpenAIGateway

# Google states how long to wait in the body of a 429, not in a header.
_RETRY_DELAY = re.compile(
    r"retry(?:Delay)?['\"]?\s*[:=]?\s*(?:in\s*)?['\"]?(\d+(?:\.\d+)?)\s*s", re.I
)
_TRUNCATED = frozenset({"MAX_TOKENS", "length"})
_REFUSED = frozenset(
    {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "content_filter"}
)


def _status(exc: Exception) -> int | None:
    for name in ("code", "status_code"):
        value = getattr(exc, name, None)
        if isinstance(value, int):
            return value
    return None


def classify(exc: Exception) -> LLMError:
    """Map a Google or transport exception to an `LLMError`. Its message is not copied."""
    if isinstance(exc, LLMError):
        return exc
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        return LLMTimeoutError()
    if isinstance(exc, ContextOverflowError):
        return LLMRejectedError()
    status = _status(exc)
    name = type(exc).__name__
    if status == 429 or name == "GoogleRateLimitError":
        found = _RETRY_DELAY.search(str(exc))
        return LLMRateLimitedError(retry_after_s=float(found.group(1)) if found else None)
    if status == 504:
        return LLMTimeoutError()
    if isinstance(exc, google_errors.ServerError) or (status is not None and status >= 500):
        return LLMUnavailableError()
    if isinstance(exc, google_errors.ClientError) or (status is not None and status >= 400):
        return LLMRejectedError()
    if name.startswith("Google") and name.endswith("Error"):
        # Authentication, permission, invalid request, model not found.
        return LLMRejectedError()
    if isinstance(exc, httpx.TransportError):
        return LLMUnavailableError()
    return LLMError()


class GeminiGateway(OpenAIGateway):
    key_name = "GEMINI_API_KEY"

    def _provider_client(self, config: ModelConfig) -> Any:
        options: dict[str, Any] = {
            "model": config.model,
            "api_key": self._api_key,
            "timeout": config.timeout_s,
            "max_retries": 0,  # retries are handled, counted and recorded by the gateway
            "max_output_tokens": config.max_output_tokens,
        }
        if config.temperature is not None:
            options["temperature"] = config.temperature
        return ChatGoogleGenerativeAI(**options)

    def _provider_chain(self, client: Any, schema: type[BaseModel]) -> Any:
        return client.with_structured_output(schema, method="json_schema", include_raw=True)

    def _provider_error(self, exc: Exception) -> LLMError:
        return classify(exc)

    def _provider_verdict(self, raw: Any) -> LLMError | None:
        reason = (getattr(raw, "response_metadata", None) or {}).get("finish_reason")
        if reason in _TRUNCATED:
            return LLMOutputTruncatedError()
        if reason in _REFUSED:
            return LLMRefusalError()
        return None
