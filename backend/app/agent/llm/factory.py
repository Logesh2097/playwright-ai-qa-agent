"""Builds the configured gateway. The one place that chooses the provider."""

import httpx

from app.agent.llm.config import ModelRegistry, RetryPolicy
from app.agent.llm.openai_gateway import OpenAIGateway
from app.core.config import Settings


def build_gateway(
    settings: Settings, *, http_client: httpx.AsyncClient | None = None
) -> OpenAIGateway:
    models = ModelRegistry.from_settings(settings)
    retry = RetryPolicy(
        max_attempts=settings.llm_max_attempts,
        base_delay_s=settings.llm_retry_base_delay_s,
        max_delay_s=settings.llm_retry_max_delay_s,
    )
    if settings.llm_provider == "gemini":
        # Imported here so an OpenAI installation never loads Google's client.
        from app.agent.llm.gemini_gateway import GeminiGateway

        return GeminiGateway(
            api_key=settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else None,
            models=models,
            retry=retry,
            requests_per_minute=settings.llm_requests_per_minute,
        )
    return OpenAIGateway(
        api_key=settings.openai_api_key.get_secret_value() if settings.openai_api_key else None,
        base_url=settings.openai_base_url,
        models=models,
        retry=retry,
        http_client=http_client,
        requests_per_minute=settings.llm_requests_per_minute,
    )
