"""Which model serves which purpose, and with what limits.

Everything here comes from settings. No model name, price or limit is written
into business logic, so changing a model is a configuration change.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.agent.llm.base import LLMUsage
from app.core.config import Settings

_PER_MILLION = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class ModelConfig:
    model: str
    timeout_s: float
    max_output_tokens: int
    temperature: float | None
    input_price_per_mtok: Decimal | None
    output_price_per_mtok: Decimal | None

    def usage(self, input_tokens: int, output_tokens: int) -> LLMUsage:
        cost = None
        if self.input_price_per_mtok is not None and self.output_price_per_mtok is not None:
            cost = (
                Decimal(input_tokens) * self.input_price_per_mtok
                + Decimal(output_tokens) * self.output_price_per_mtok
            ) / _PER_MILLION
        return LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int
    base_delay_s: float
    max_delay_s: float


class ModelRegistry:
    """Maps a purpose ("analysis", later "generation", ...) to its model configuration."""

    def __init__(self, models: dict[str, ModelConfig]) -> None:
        self._models = models

    @classmethod
    def from_settings(cls, settings: Settings) -> "ModelRegistry":
        models: dict[str, ModelConfig] = {}
        if settings.llm_model_analysis:
            models["analysis"] = ModelConfig(
                model=settings.llm_model_analysis,
                timeout_s=settings.llm_timeout_s,
                max_output_tokens=settings.llm_max_output_tokens,
                temperature=settings.llm_temperature,
                input_price_per_mtok=settings.llm_input_price_per_mtok,
                output_price_per_mtok=settings.llm_output_price_per_mtok,
            )
            # Writing cases produces far more output than analysing a requirement.
            models["generation"] = ModelConfig(
                model=settings.llm_model_generation or settings.llm_model_analysis,
                timeout_s=settings.llm_timeout_s * 2,
                max_output_tokens=settings.llm_generation_max_output_tokens,
                temperature=settings.llm_temperature,
                input_price_per_mtok=settings.llm_input_price_per_mtok,
                output_price_per_mtok=settings.llm_output_price_per_mtok,
            )
        return cls(models)

    def get(self, purpose: str) -> ModelConfig | None:
        return self._models.get(purpose)
