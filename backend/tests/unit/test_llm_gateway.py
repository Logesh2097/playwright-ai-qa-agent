"""The OpenAI gateway, exercised through the real SDK against simulated HTTP responses."""

import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest

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
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.agent.llm.config import ModelConfig, ModelRegistry, RetryPolicy
from app.agent.llm.factory import build_gateway
from app.agent.llm.openai_gateway import OpenAIGateway
from app.agent.schemas.requirement import RequirementAnalysis
from tests.conftest import make_settings
from tests.factories import analysis_dict, api_error, completion, ok

API_KEY = "sk-test-key-that-must-never-leak"
Responder = Callable[[httpx.Request], httpx.Response]


class Provider:
    """Scripted stand-in for the provider's HTTP API."""

    def __init__(self, *responses: httpx.Response | Exception | Responder) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.headers.append(request.headers)
        step = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(step, Exception):
            raise step
        return step(request) if callable(step) else step


def gateway(
    provider: Provider, *, sleeps: list[float] | None = None, **model: Any
) -> OpenAIGateway:
    config: dict[str, Any] = {
        "model": "test-model",
        "timeout_s": 5.0,
        "max_output_tokens": 2000,
        "temperature": None,
        "input_price_per_mtok": None,
        "output_price_per_mtok": None,
    }
    config.update(model)

    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return OpenAIGateway(
        api_key=API_KEY,
        models=ModelRegistry({"analysis": ModelConfig(**config)}),
        retry=RetryPolicy(max_attempts=3, base_delay_s=1.0, max_delay_s=20.0),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider)),
        sleep=sleep,
    )


def request(recorder: Any = None) -> LLMRequest[RequirementAnalysis]:
    return LLMRequest(
        purpose="analysis",
        system="SYSTEM RULES",
        user="USER DATA",
        schema=RequirementAnalysis,
        prompt="requirement_analysis@v1",
        recorder=recorder,
    )


# --- success -----------------------------------------------------------------------


async def test_returns_validated_output_with_usage() -> None:
    provider = Provider(ok(prompt_tokens=1200, completion_tokens=300))
    result = await gateway(provider).structured(request())

    assert isinstance(result.output, RequirementAnalysis)
    assert result.output.feature_name == "Customer login"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (1200, 300)
    assert result.usage.cost_usd is None, "no price configured, so no cost is claimed"
    assert (result.calls, result.model) == (1, "test-model")


async def test_cost_is_computed_from_configured_prices() -> None:
    provider = Provider(ok(prompt_tokens=1_000_000, completion_tokens=500_000))
    result = await gateway(
        provider, input_price_per_mtok=Decimal("2.50"), output_price_per_mtok=Decimal("10")
    ).structured(request())
    assert result.usage.cost_usd == Decimal("7.50")


async def test_request_uses_the_configured_model_and_a_strict_schema() -> None:
    provider = Provider(ok())
    await gateway(provider, model="some-model", max_output_tokens=1234).structured(request())
    body = provider.requests[0]

    assert body["model"] == "some-model"
    assert body["max_completion_tokens"] == 1234
    assert "temperature" not in body, "not sent unless configured"
    assert body["messages"] == [
        {"role": "system", "content": "SYSTEM RULES"},
        {"role": "user", "content": "USER DATA"},
    ]
    response_format = body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert provider.headers[0]["authorization"] == f"Bearer {API_KEY}"

    configured = Provider(ok())
    await gateway(configured, temperature=0.0).structured(request())
    assert configured.requests[0]["temperature"] == 0.0


async def test_schema_sent_to_the_provider_satisfies_strict_mode_rules() -> None:
    """Strict mode needs every property required, no extra properties, and no length keywords."""
    provider = Provider(ok())
    await gateway(provider).structured(request())
    schema = provider.requests[0]["response_format"]["json_schema"]["schema"]

    unsupported = {"minLength", "maxLength", "minItems", "maxItems", "default", "format"}
    objects = 0

    def walk(node: Any) -> None:
        nonlocal objects
        if isinstance(node, dict):
            assert not unsupported & set(node), f"unsupported keyword in {sorted(node)}"
            if node.get("type") == "object":
                objects += 1
                assert node.get("additionalProperties") is False
                assert sorted(node.get("required", [])) == sorted(node.get("properties", {}))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    assert objects >= 8, "the nested models are part of the schema"


# --- output that does not fit the structure ------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "this is not JSON",
        json.dumps({"summary": "only one field"}),
        # an unknown field: the model may not add anything, such as an approval
        json.dumps({**analysis_dict(), "approved": True}),
        # an invented result presented with source "unknown"
        json.dumps(
            analysis_dict(
                expected_behavior=[
                    {
                        "objective_id": "OBJ-1",
                        "description": "Redirects to /home",
                        "source": "unknown",
                    }
                ]
            )
        ),
    ],
)
async def test_invalid_output_gets_one_repair_attempt(bad: str) -> None:
    provider = Provider(httpx.Response(200, json=completion(bad)), ok())
    records: list[LLMCallRecord] = []

    async def recorder(record: LLMCallRecord) -> None:
        records.append(record)

    result = await gateway(provider).structured(request(recorder))

    assert result.calls == 2
    assert [r.status for r in records] == ["llm_invalid_output", "ok"]
    repair = provider.requests[1]["messages"]
    assert repair[0]["content"] == "SYSTEM RULES", "the rules are repeated unchanged"
    assert "did not satisfy the required structure" in repair[-1]["content"]
    assert "Redirects to /home" not in json.dumps(repair), "the rejected text is not fed back"


async def test_output_that_stays_invalid_is_rejected_not_accepted() -> None:
    provider = Provider(httpx.Response(200, json=completion("still not JSON")))
    with pytest.raises(LLMInvalidOutputError) as excinfo:
        await gateway(provider).structured(request())
    assert excinfo.value.code == "llm_invalid_output"
    assert len(provider.requests) == 2, "exactly one repair attempt, then give up"


async def test_refusal_and_truncation_are_reported_without_retrying() -> None:
    refused = Provider(httpx.Response(200, json=completion(None, refusal="I cannot help")))
    with pytest.raises(LLMRefusalError):
        await gateway(refused).structured(request())
    assert len(refused.requests) == 1

    truncated = Provider(httpx.Response(200, json=completion('{"summary": "cut', finish="length")))
    with pytest.raises(LLMOutputTruncatedError):
        await gateway(truncated).structured(request())
    assert len(truncated.requests) == 1


# --- provider failures -----------------------------------------------------------------


async def test_rate_limit_is_retried_honouring_retry_after() -> None:
    sleeps: list[float] = []
    provider = Provider(api_error(429, **{"retry-after": "7"}), ok())
    result = await gateway(provider, sleeps=sleeps).structured(request())
    assert result.calls == 2
    assert len(sleeps) == 1 and sleeps[0] >= 7


async def test_server_errors_are_retried_with_growing_backoff_then_reported() -> None:
    sleeps: list[float] = []
    provider = Provider(api_error(503))
    with pytest.raises(LLMUnavailableError) as excinfo:
        await gateway(provider, sleeps=sleeps).structured(request())
    assert excinfo.value.code == "llm_unavailable"
    assert len(provider.requests) == 3, "max_attempts"
    assert len(sleeps) == 2 and 0.5 <= sleeps[0] <= 1.0 and 1.0 <= sleeps[1] <= 2.0


async def test_recovers_when_a_retry_succeeds() -> None:
    provider = Provider(api_error(500), httpx.ConnectError("refused"), ok())
    result = await gateway(provider, sleeps=[]).structured(request())
    assert result.calls == 3


async def test_timeouts_are_retried_then_reported_as_timeout() -> None:
    provider = Provider(httpx.ReadTimeout("slow"))
    records: list[LLMCallRecord] = []

    async def recorder(record: LLMCallRecord) -> None:
        records.append(record)

    with pytest.raises(LLMTimeoutError) as excinfo:
        await gateway(provider, sleeps=[]).structured(request(recorder))
    assert excinfo.value.code == "llm_timeout"
    assert [r.status for r in records] == ["llm_timeout"] * 3
    assert [r.attempt for r in records] == [1, 2, 3]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_rejected_requests_are_not_retried_and_do_not_leak_provider_text(status: int) -> None:
    provider = Provider(api_error(status, f"Incorrect API key provided: {API_KEY}"))
    with pytest.raises(LLMRejectedError) as excinfo:
        await gateway(provider, sleeps=[]).structured(request())
    assert len(provider.requests) == 1
    assert API_KEY not in str(excinfo.value)
    assert "Incorrect API key" not in str(excinfo.value)
    assert excinfo.value.code == "llm_request_rejected"


async def test_rate_limit_that_persists_is_reported() -> None:
    provider = Provider(api_error(429))
    with pytest.raises(LLMRateLimitedError):
        await gateway(provider, sleeps=[]).structured(request())
    assert len(provider.requests) == 3


async def test_every_call_is_recorded_including_failures() -> None:
    provider = Provider(api_error(500), ok(prompt_tokens=10, completion_tokens=5))
    records: list[LLMCallRecord] = []

    async def recorder(record: LLMCallRecord) -> None:
        records.append(record)

    await gateway(provider, sleeps=[]).structured(request(recorder))
    assert [(r.status, r.usage.input_tokens, r.model) for r in records] == [
        ("llm_unavailable", 0, "test-model"),
        ("ok", 10, "test-model"),
    ]
    assert all(r.prompt == "requirement_analysis@v1" and r.purpose == "analysis" for r in records)


async def test_endpoint_is_explicit_even_when_the_environment_variable_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compose passes unset variables as empty strings; the SDK must not pick that up."""
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    urls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return ok()

    await gateway(Provider(respond)).structured(request())
    assert urls == ["https://api.openai.com/v1/chat/completions"]

    custom = OpenAIGateway(
        api_key=API_KEY,
        base_url="https://llm-proxy.internal.example/v1",
        models=ModelRegistry({"analysis": ModelConfig("m", 5.0, 1000, None, None, None)}),
        retry=RetryPolicy(1, 0, 0),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(Provider(respond))),
    )
    await custom.structured(request())
    assert urls[-1] == "https://llm-proxy.internal.example/v1/chat/completions"


# --- configuration ---------------------------------------------------------------------


async def test_without_a_key_nothing_is_sent() -> None:
    provider = Provider(ok())
    unconfigured = build_gateway(
        make_settings(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider))
    )
    assert unconfigured.configured is False
    with pytest.raises(LLMNotConfiguredError):
        await unconfigured.structured(request())
    assert provider.requests == []


def test_gateway_is_built_from_settings_only() -> None:
    settings = make_settings(
        openai_api_key=API_KEY,
        llm_model_analysis="configured-model",
        llm_input_price_per_mtok="1.5",
        llm_output_price_per_mtok="6",
    )
    assert build_gateway(settings).configured is True
    config = ModelRegistry.from_settings(settings).get("analysis")
    assert config is not None
    assert (config.model, config.timeout_s) == ("configured-model", 60.0)
    assert config.usage(2_000_000, 1_000_000).cost_usd == Decimal("9.0")
    assert API_KEY not in repr(settings)


def test_a_key_without_a_model_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="llm_model_analysis"):
        make_settings(openai_api_key=API_KEY)
    # Blank values from the environment count as unset.
    blank = make_settings(openai_api_key="", llm_model_analysis=" ", llm_temperature="")
    assert blank.openai_api_key is None and blank.llm_model_analysis is None


def test_error_types_are_distinct_and_user_safe() -> None:
    errors: list[type[LLMError]] = [
        LLMTimeoutError,
        LLMUnavailableError,
        LLMRateLimitedError,
        LLMRejectedError,
        LLMInvalidOutputError,
        LLMRefusalError,
        LLMOutputTruncatedError,
        LLMNotConfiguredError,
    ]
    assert len({e.code for e in errors}) == len(errors)
    assert [e.retryable for e in errors] == [True, True, True, False, False, False, False, False]
