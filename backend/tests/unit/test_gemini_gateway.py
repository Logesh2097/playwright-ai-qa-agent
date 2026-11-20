"""The Gemini gateway: provider choice, Google's errors, and request pacing.

Google's client is not driven over HTTP here. The model call is replaced by a
scripted one, so what is tested is what this project adds: which gateway is
built, how each kind of failure is read, and that nothing of the provider's
text or the key reaches the caller.
"""

from typing import Any

import httpx
import pytest
from google.genai import errors as google_errors
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from app.agent.llm.base import (
    LLMError,
    LLMInvalidOutputError,
    LLMNotConfiguredError,
    LLMOutputTruncatedError,
    LLMRateLimitedError,
    LLMRefusalError,
    LLMRejectedError,
    LLMRequest,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.agent.llm.config import ModelConfig, ModelRegistry, RetryPolicy
from app.agent.llm.factory import build_gateway
from app.agent.llm.gemini_gateway import GeminiGateway, classify
from app.agent.llm.openai_gateway import OpenAIGateway, Pacer
from tests.conftest import make_settings

API_KEY = "test-gemini-key-that-must-never-leak"


class Answer(BaseModel):
    text: str


class Scripted(GeminiGateway):
    """A Gemini gateway whose model call returns, or raises, what the test scripted."""

    def __init__(self, *steps: Any, sleeps: list[float] | None = None, rpm: int = 0) -> None:
        async def sleep(seconds: float) -> None:
            if sleeps is not None:
                sleeps.append(seconds)

        super().__init__(
            api_key=API_KEY,
            models=ModelRegistry(
                {"analysis": ModelConfig("gemini-test", 5.0, 1000, 0.0, None, None)}
            ),
            retry=RetryPolicy(max_attempts=3, base_delay_s=1.0, max_delay_s=20.0),
            sleep=sleep,
            requests_per_minute=rpm,
        )
        self.steps = list(steps)
        self.calls = 0
        outer = self

        class Chain:
            async def ainvoke(self, messages: Any) -> Any:
                outer.calls += 1
                step = outer.steps.pop(0) if len(outer.steps) > 1 else outer.steps[0]
                if isinstance(step, Exception):
                    raise step
                return step

        self._chain = Chain()

    def _provider_chain(self, client: Any, schema: type[BaseModel]) -> Any:
        return self._chain


def answer(finish_reason: str = "STOP", parsed: Any = None) -> dict[str, Any]:
    raw = AIMessage(
        content="",
        response_metadata={"finish_reason": finish_reason},
        usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
    )
    return {"raw": raw, "parsed": parsed, "parsing_error": None}


def request() -> LLMRequest[Answer]:
    return LLMRequest(purpose="analysis", prompt="test@v1", system="s", user="u", schema=Answer)


def google(code: int, message: str = "") -> google_errors.APIError:
    kind = google_errors.ServerError if code >= 500 else google_errors.ClientError
    return kind(code, {"error": {"code": code, "message": message, "status": "X"}})


# --- provider choice -------------------------------------------------------------------


def test_the_provider_setting_chooses_the_gateway_and_its_key() -> None:
    gemini = build_gateway(
        make_settings(llm_provider="gemini", gemini_api_key=API_KEY, llm_model_analysis="g")
    )
    assert type(gemini) is GeminiGateway and gemini.configured is True

    openai = build_gateway(make_settings(openai_api_key="sk-test", llm_model_analysis="m"))
    assert type(openai) is OpenAIGateway and openai.configured is True


def test_the_other_providers_key_does_not_configure_anything() -> None:
    # A Gemini installation with only an OpenAI key has no model, and the reverse.
    gemini = build_gateway(make_settings(llm_provider="gemini", openai_api_key="sk-test"))
    assert type(gemini) is GeminiGateway and gemini.configured is False
    openai = build_gateway(make_settings(gemini_api_key=API_KEY))
    assert type(openai) is OpenAIGateway and openai.configured is False


async def test_without_a_gemini_key_nothing_is_called() -> None:
    unconfigured = build_gateway(make_settings(llm_provider="gemini"))
    with pytest.raises(LLMNotConfiguredError):
        await unconfigured.structured(request())


def test_a_gemini_key_needs_a_model_and_is_never_shown() -> None:
    with pytest.raises(ValueError, match="llm_model_analysis"):
        make_settings(llm_provider="gemini", gemini_api_key=API_KEY)
    settings = make_settings(llm_provider="gemini", gemini_api_key=API_KEY, llm_model_analysis="g")
    assert API_KEY not in repr(settings)
    assert make_settings(llm_provider="gemini", gemini_api_key=" ").gemini_api_key is None
    with pytest.raises(ValueError):
        make_settings(llm_provider="someone-else")


# --- reading Google's errors -----------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (google(429, "Quota exceeded. Please retry in 37.5s."), LLMRateLimitedError),
        (google(500), LLMUnavailableError),
        (google(503, "The model is overloaded."), LLMUnavailableError),
        (google(504), LLMTimeoutError),
        (google(400, "Invalid JSON schema"), LLMRejectedError),
        (google(401), LLMRejectedError),
        (google(403, f"API key {API_KEY} not valid"), LLMRejectedError),
        (google(404, "model not found"), LLMRejectedError),
        (httpx.ReadTimeout("slow"), LLMTimeoutError),
        (TimeoutError(), LLMTimeoutError),
        (httpx.ConnectError("no route"), LLMUnavailableError),
        (RuntimeError("anything else"), LLMError),
    ],
)
def test_each_failure_is_read_as_the_right_kind(exc: Exception, expected: type[LLMError]) -> None:
    error = classify(exc)
    assert type(error) is expected
    # The provider's words, and anything a key could ride in on, are not passed along.
    assert API_KEY not in str(error) and "Quota" not in str(error)


def test_the_wait_google_asks_for_is_read_from_the_message() -> None:
    error = classify(google(429, "Quota exceeded. Please retry in 37.5s."))
    assert isinstance(error, LLMRateLimitedError) and error.retry_after_s == 37.5
    structured = classify(google(429, "{'retryDelay': '12s'}"))
    assert isinstance(structured, LLMRateLimitedError) and structured.retry_after_s == 12.0
    silent = classify(google(429, "Too many requests"))
    assert isinstance(silent, LLMRateLimitedError) and silent.retry_after_s is None


# --- behaviour through the gateway -----------------------------------------------------


async def test_a_valid_answer_is_returned_with_its_usage() -> None:
    gateway = Scripted(answer(parsed=Answer(text="hello")))
    result = await gateway.structured(request())
    assert result.output == Answer(text="hello") and result.model == "gemini-test"
    assert (result.usage.input_tokens, result.usage.output_tokens, result.calls) == (11, 7, 1)


async def test_a_rate_limit_is_waited_out_and_retried() -> None:
    sleeps: list[float] = []
    gateway = Scripted(
        google(429, "Please retry in 9s."), answer(parsed=Answer(text="ok")), sleeps=sleeps
    )
    result = await gateway.structured(request())
    assert result.calls == 2 and sleeps and sleeps[0] >= 9.0


async def test_a_rejected_request_is_not_retried() -> None:
    gateway = Scripted(google(400, "bad schema"))
    with pytest.raises(LLMRejectedError):
        await gateway.structured(request())
    assert gateway.calls == 1


async def test_retries_stop_at_the_limit() -> None:
    gateway = Scripted(google(503))
    with pytest.raises(LLMUnavailableError):
        await gateway.structured(request())
    assert gateway.calls == 3


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [
        ("MAX_TOKENS", LLMOutputTruncatedError),
        ("SAFETY", LLMRefusalError),
        ("RECITATION", LLMRefusalError),
        ("PROHIBITED_CONTENT", LLMRefusalError),
    ],
)
async def test_a_cut_off_or_blocked_answer_is_not_treated_as_output(
    finish_reason: str, expected: type[LLMError]
) -> None:
    # Even if something parseable came back with it.
    gateway = Scripted(answer(finish_reason, parsed=Answer(text="partial")))
    with pytest.raises(expected):
        await gateway.structured(request())


async def test_an_answer_that_is_not_the_schema_is_refused_after_one_repair() -> None:
    gateway = Scripted(answer(parsed=None))
    with pytest.raises(LLMError):
        await gateway.structured(request())
    assert gateway.calls <= 3


def unparseable(text: str = "the model's own words") -> dict[str, Any]:
    """What Google's client hands back when the answer is not the schema."""
    try:
        Answer.model_validate({"wrong": 1})
    except Exception as cause:
        error = OutputParserException(f"Failed to parse Answer from completion {text}")
        error.__cause__ = cause
    return {**answer(), "parsing_error": error}


async def test_an_unparseable_answer_gets_one_repair_attempt() -> None:
    """Found against the real service: the parser error was treated as an unknown failure."""
    gateway = Scripted(unparseable(), answer(parsed=Answer(text="second try")))
    result = await gateway.structured(request())
    assert result.output == Answer(text="second try") and result.calls == 2


async def test_a_second_unparseable_answer_is_invalid_output_without_the_models_text() -> None:
    gateway = Scripted(unparseable("SECRET-FROM-MODEL"))
    with pytest.raises(LLMInvalidOutputError) as caught:
        await gateway.structured(request())
    assert gateway.calls == 2
    assert "SECRET-FROM-MODEL" not in str(caught.value)
    assert "SECRET-FROM-MODEL" not in getattr(caught.value, "problems", "")


async def test_a_parser_error_raised_instead_of_returned_is_read_the_same_way() -> None:
    gateway = Scripted(OutputParserException("not JSON at all"), answer(parsed=Answer(text="ok")))
    assert (await gateway.structured(request())).calls == 2


# --- pacing ----------------------------------------------------------------------------


async def test_requests_beyond_the_quota_wait_for_the_window() -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        pacer._sent.clear()  # the minute has passed

    pacer = Pacer(2, sleep=sleep)
    await pacer.wait()
    await pacer.wait()
    assert sleeps == []
    await pacer.wait()
    assert len(sleeps) == 1 and 59 < sleeps[0] <= 60.1


async def test_no_limit_means_no_waiting() -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    pacer = Pacer(0, sleep=sleep)
    for _ in range(50):
        await pacer.wait()
    assert sleeps == []


async def test_the_gateway_paces_its_calls() -> None:
    sleeps: list[float] = []
    gateway = Scripted(answer(parsed=Answer(text="x")), sleeps=sleeps, rpm=1)
    await gateway.structured(request())
    assert sleeps == []
    gateway._pacer._sleep = _advance(gateway, sleeps)
    await gateway.structured(request())
    assert len(sleeps) == 1


def _advance(gateway: Scripted, sleeps: list[float]) -> Any:
    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        gateway._pacer._sent.clear()

    return sleep
