"""Official model pricing and cost estimation helpers.

Pricing sources checked on 2026-04-06:
- OpenAI API pricing: https://openai.com/api/pricing/
- Anthropic Claude Opus 4.6: https://www.anthropic.com/claude/opus
- Anthropic Claude Sonnet 4.6: https://www.anthropic.com/news/claude-sonnet-4-6

Anthropic cache read/write prices are inferred from Anthropic's documented
prompt caching multipliers (read = 0.1x input, 5m write = 1.25x input).
For GPT-5.4, this proxy prices cache writes at the standard input rate and
cache reads at OpenAI's official cached-input rate.
"""

from copy import deepcopy

PRICING_AS_OF = "2026-04-06"
DEFAULT_MODEL = "claude-opus-4-6"
PRIMARY_MODELS = [
    DEFAULT_MODEL,
    "claude-sonnet-4-6",
    "gpt-5.4",
]

MODEL_ALIASES = {
    "gpt5.4": "gpt-5.4",
}

MODEL_PRICING = {
    "claude-opus-4-6": {
        "model": "claude-opus-4-6",
        "display_name": "Claude Opus 4.6",
        "provider": "Anthropic",
        "input_price_per_m": 5.0,
        "cache_read_price_per_m": 0.5,
        "cache_write_price_per_m": 6.25,
        "output_price_per_m": 25.0,
        "source_url": "https://www.anthropic.com/claude/opus",
        "source_note": "Official input/output pricing; cache pricing inferred from Anthropic prompt caching multipliers.",
        "is_default": True,
    },
    "claude-sonnet-4-6": {
        "model": "claude-sonnet-4-6",
        "display_name": "Claude Sonnet 4.6",
        "provider": "Anthropic",
        "input_price_per_m": 3.0,
        "cache_read_price_per_m": 0.3,
        "cache_write_price_per_m": 3.75,
        "output_price_per_m": 15.0,
        "source_url": "https://www.anthropic.com/news/claude-sonnet-4-6",
        "source_note": "Official input/output pricing; cache pricing inferred from Anthropic prompt caching multipliers.",
        "is_default": False,
    },
    "gpt-5.4": {
        "model": "gpt-5.4",
        "display_name": "GPT-5.4",
        "provider": "OpenAI",
        "input_price_per_m": 2.5,
        "cache_read_price_per_m": 0.25,
        "cache_write_price_per_m": 2.5,
        "output_price_per_m": 15.0,
        "source_url": "https://openai.com/api/pricing/",
        "source_note": "Official input/cached-input/output pricing; cache writes are charged at the standard input rate in this proxy.",
        "is_default": False,
    },
}


def normalize_model_name(model: str) -> str:
    if not model:
        return ""
    lower = model.lower()
    return MODEL_ALIASES.get(lower, lower)


def get_model_pricing(model: str) -> dict | None:
    entry = MODEL_PRICING.get(normalize_model_name(model))
    return deepcopy(entry) if entry else None


def get_pricing_catalog() -> list[dict]:
    return [deepcopy(MODEL_PRICING[name]) for name in PRIMARY_MODELS if name in MODEL_PRICING]


def estimate_usage_cost_usd(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    pricing = get_model_pricing(model)
    if not pricing:
        return 0.0

    total = 0.0
    total += (input_tokens / 1_000_000) * pricing["input_price_per_m"]
    total += (output_tokens / 1_000_000) * pricing["output_price_per_m"]
    total += (cache_read_tokens / 1_000_000) * pricing["cache_read_price_per_m"]
    total += (cache_write_tokens / 1_000_000) * pricing["cache_write_price_per_m"]
    return round(total, 6)
