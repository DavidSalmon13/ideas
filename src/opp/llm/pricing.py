"""Per-model token prices, for metering every call (plan.md §16, §22).

First-party Anthropic list prices in USD per million tokens. Cache writes (5-minute TTL)
cost 1.25x input and cache reads 0.1x. A model missing from this table can't be routed
to: an unpriced call would slip past the budget meter.
"""

from dataclasses import dataclass

from opp.llm.types import Usage

CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1
# Rough English average, used only for pre-flight estimates before a call is made.
CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float


PRICES: dict[str, ModelPrice] = {
    "claude-haiku-4-5": ModelPrice(1.00, 5.00),
    "claude-sonnet-5": ModelPrice(2.00, 10.00),
    "claude-sonnet-4-6": ModelPrice(3.00, 15.00),
    "claude-opus-5": ModelPrice(5.00, 25.00),
    "claude-opus-4-8": ModelPrice(5.00, 25.00),
}


class UnknownModelError(KeyError):
    pass


def price_for(model: str) -> ModelPrice:
    try:
        return PRICES[model]
    except KeyError:
        raise UnknownModelError(f"no price configured for model {model!r}") from None


def cost_usd(model: str, usage: Usage) -> float:
    price = price_for(model)
    input_cost = (
        usage.input_tokens
        + usage.cache_write_tokens * CACHE_WRITE_MULTIPLIER
        + usage.cache_read_tokens * CACHE_READ_MULTIPLIER
    ) * price.input_per_mtok
    return (input_cost + usage.output_tokens * price.output_per_mtok) / 1_000_000


def estimate_max_cost_usd(model: str, *, prompt_chars: int, max_tokens: int) -> float:
    """Upper-bound pre-flight estimate: the prompt at ~4 chars/token, all output used."""
    input_tokens = -(-prompt_chars // CHARS_PER_TOKEN)  # ceil
    return cost_usd(model, Usage(input_tokens=input_tokens, output_tokens=max_tokens))
