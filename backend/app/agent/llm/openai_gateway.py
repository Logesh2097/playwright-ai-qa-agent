"""OpenAI implementation of `LLMGateway`, built on LangChain's chat model.

What this class adds on top of the SDK:

- output is requested with a strict JSON schema and then validated with Pydantic
  (schemas forbid unknown fields), so nothing unvalidated reaches the caller;
- one repair attempt when the output fails validation;
- its own retry loop with backoff for timeouts, connection errors, 429 and 5xx
  (the SDK's built-in retries are switched off so every call is counted);
- provider errors mapped to a small set of `LLMError` types whose messages never
  contain provider text, keys or prompt content;
- a record of every call (tokens, cost, latency, outcome) handed to the caller.
"""

import asyncio
import logging
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import openai
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from app.agent.llm.base import (
    LLMCallRecord,
    LLMError,
    LLMInvalidOutputError,
    LLMNotConfiguredError,
    LLMOutputTruncatedError,
    LLMRateLimitedError,
    LLMRefusalError,
    LLMRejectedError,
    LLMRequest,
    LLMResult,
    LLMTimeoutError,
    LLMUnavailableError,
    LLMUsage,
)
from app.agent.llm.config import ModelConfig, ModelRegistry, RetryPolicy

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]

DEFAULT_BASE_URL = "https://api.openai.com/v1"

_REPAIR_NOTE = (
    "Your previous answer was rejected because it did not satisfy the required structure:\n"
    "{problems}\n"
    "Answer again. Follow the schema exactly and keep every earlier rule."
)


def _classify(exc: Exception) -> LLMError:
    """Map a provider exception to an `LLMError`. The provider's message is not copied."""
    if isinstance(exc, LLMError):
        return exc
    if isinstance(exc, openai.LengthFinishReasonError):
        return LLMOutputTruncatedError()
    if isinstance(exc, openai.ContentFilterFinishReasonError):
        return LLMRefusalError()
    if isinstance(exc, openai.APITimeoutError | httpx.TimeoutException | TimeoutError):
        return LLMTimeoutError()

    status = getattr(exc, "status_code", None)
    if status == 429:
        return LLMRateLimitedError(retry_after_s=_retry_after(exc))
    if isinstance(status, int) and status >= 500:
        return LLMUnavailableError()
    if isinstance(status, int) and status >= 400:
        return LLMRejectedError()
    if isinstance(exc, openai.APIConnectionError | httpx.TransportError):
        return LLMUnavailableError()
    return LLMError()


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    value = response.headers.get("retry-after") if response is not None else None
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None


def _validation_summary(exc: ValidationError) -> str:
    """Field paths and messages only. Values are left out: they are model output."""
    lines = []
    for error in exc.errors(include_input=False, include_url=False)[:12]:
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        lines.append(f"- {location}: {error['msg']}")
    return "\n".join(lines)


class Pacer:
    """Keeps one process under a requests-per-minute quota by waiting before a call.

    A provider's quota is per key, and the API and the worker are separate processes,
    so set the limit with room to spare. Zero switches pacing off.
    """

    def __init__(self, per_minute: int, *, sleep: Sleep = asyncio.sleep) -> None:
        self._per_minute = per_minute
        self._sleep = sleep
        self._sent: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self._per_minute <= 0:
            return
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._sent and now - self._sent[0] >= 60:
                    self._sent.popleft()
                if len(self._sent) < self._per_minute:
                    self._sent.append(now)
                    return
                await self._sleep(60 - (now - self._sent[0]) + 0.05)


class OpenAIGateway:
    """The gateway. A subclass for another provider replaces the three `_provider` hooks."""

    key_name = "OPENAI_API_KEY"

    def __init__(
        self,
        *,
        api_key: str | None,
        models: ModelRegistry,
        retry: RetryPolicy,
        base_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        requests_per_minute: int = 0,
    ) -> None:
        self._api_key = api_key
        self._models = models
        self._retry = retry
        self._base_url = base_url
        self._http_client = http_client
        self._sleep = sleep
        self._pacer = Pacer(requests_per_minute, sleep=sleep)
        self._clients: dict[str, Any] = {}

    @property
    def configured(self) -> bool:
        return bool(self._api_key) and self._models.get("analysis") is not None

    def _client(self, purpose: str, config: ModelConfig) -> Any:
        if purpose not in self._clients:
            self._clients[purpose] = self._provider_client(config)
        return self._clients[purpose]

    def _provider_client(self, config: ModelConfig) -> Any:
        options: dict[str, Any] = {
            "model": config.model,
            "api_key": self._api_key,
            # Always explicit. Left to the SDK, an OPENAI_BASE_URL variable that is set
            # but empty (as container tooling does for unset values) yields no endpoint.
            "base_url": self._base_url or DEFAULT_BASE_URL,
            "timeout": config.timeout_s,
            "max_retries": 0,  # retries are handled, counted and recorded here
            "max_completion_tokens": config.max_output_tokens,
            "http_async_client": self._http_client,
            "use_responses_api": False,
        }
        if config.temperature is not None:
            options["temperature"] = config.temperature
        return ChatOpenAI(**options)

    def _provider_chain(self, client: Any, schema: type[BaseModel]) -> Any:
        return client.with_structured_output(
            schema, method="json_schema", strict=True, include_raw=True
        )

    def _provider_error(self, exc: Exception) -> LLMError:
        return _classify(exc)

    def _provider_verdict(self, raw: Any) -> LLMError | None:
        """An error the provider reported inside an answer rather than by raising."""
        if getattr(raw, "additional_kwargs", {}).get("refusal"):
            return LLMRefusalError()
        return None

    async def structured[T: BaseModel](self, request: LLMRequest[T]) -> LLMResult[T]:
        config = self._models.get(request.purpose)
        if not self._api_key or config is None:
            raise LLMNotConfiguredError()

        chain = self._provider_chain(self._client(request.purpose, config), request.schema)
        messages: list[BaseMessage] = [
            SystemMessage(content=request.system),
            HumanMessage(content=request.user),
        ]
        started = time.perf_counter()
        calls = 0
        total_in = total_out = 0
        repaired = False

        while True:
            calls += 1
            output, usage, error = await self._call(chain, messages, request, config, calls)
            total_in += usage.input_tokens
            total_out += usage.output_tokens

            if output is not None:
                return LLMResult(
                    output=output,
                    model=config.model,
                    usage=config.usage(total_in, total_out),
                    calls=calls,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            assert error is not None  # noqa: S101 - exactly one of output/error is set

            if isinstance(error, _InvalidOutputError) and not repaired:
                # One chance to correct itself, told what was wrong but not shown its text.
                repaired = True
                messages = [
                    *messages[:2],
                    HumanMessage(content=_REPAIR_NOTE.format(problems=error.problems)),
                ]
                continue
            if error.retryable and calls < self._retry.max_attempts:
                await self._sleep(self._delay(calls, error))
                continue
            raise error

    async def _call[T: BaseModel](
        self,
        chain: Any,
        messages: list[BaseMessage],
        request: LLMRequest[T],
        config: ModelConfig,
        attempt: int,
    ) -> tuple[T | None, LLMUsage, LLMError | None]:
        started = time.perf_counter()
        usage = LLMUsage()
        output: T | None = None
        error: LLMError | None = None
        try:
            await self._pacer.wait()
            result = await chain.ainvoke(messages)
            raw = result.get("raw")
            metadata = getattr(raw, "usage_metadata", None) or {}
            usage = config.usage(
                int(metadata.get("input_tokens", 0)), int(metadata.get("output_tokens", 0))
            )
            verdict = self._provider_verdict(raw)
            if verdict is not None:
                error = verdict
            elif (invalid := _invalid_output(result.get("parsing_error"))) is not None:
                error = invalid
            elif isinstance(result.get("parsed"), request.schema):
                output = result["parsed"]
            else:
                error = self._provider_error(result.get("parsing_error") or LLMInvalidOutputError())
        except (ValidationError, OutputParserException) as exc:
            error = _invalid_output(exc) or LLMInvalidOutputError()
        except Exception as exc:
            error = self._provider_error(exc)
            if type(error) is LLMError:
                logger.exception("llm_unexpected_error", extra={"purpose": request.purpose})

        if request.recorder is not None:
            await request.recorder(
                LLMCallRecord(
                    purpose=request.purpose,
                    model=config.model,
                    prompt=request.prompt,
                    status="ok" if error is None else error.code,
                    usage=usage,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    attempt=attempt,
                )
            )
        return output, usage, error

    def _delay(self, attempt: int, error: LLMError) -> float:
        """Exponential backoff with jitter; a server-supplied Retry-After wins if longer."""
        doubled = self._retry.base_delay_s * float(1 << max(attempt - 1, 0))
        backoff = min(self._retry.max_delay_s, doubled)
        delay = backoff * (0.5 + random.random() / 2)  # noqa: S311 - jitter, not security
        hinted = error.retry_after_s if isinstance(error, LLMRateLimitedError) else None
        if hinted is not None:
            delay = max(delay, min(hinted, self._retry.max_delay_s))
        return delay


class _InvalidOutputError(LLMInvalidOutputError):
    def __init__(self, problems: str) -> None:
        super().__init__()
        self.problems = problems


def _invalid_output(exc: object) -> _InvalidOutputError | None:
    """An answer that is not the schema, however the provider's client reports it.

    Some clients hand back the validation error itself; others wrap it in a parser
    error whose message contains the model's text. Only field paths and messages are
    kept, never that text.
    """
    if isinstance(exc, ValidationError):
        return _InvalidOutputError(_validation_summary(exc))
    if isinstance(exc, OutputParserException):
        cause: BaseException | None = exc
        while cause is not None:
            if isinstance(cause, ValidationError):
                return _InvalidOutputError(_validation_summary(cause))
            cause = cause.__cause__ or cause.__context__
        return _InvalidOutputError("- (root): the answer was not valid JSON for the schema")
    return None
