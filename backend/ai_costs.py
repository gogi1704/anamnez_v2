"""Token usage parsing and estimated OpenAI/Yandex API cost calculation.

The database stores the rate snapshot used for every request so historical totals do
not change when the price catalog is updated.  No prompt or response text is stored.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


# USD per one million text tokens, verified against the official OpenAI and
# Yandex AI Studio pricing pages on 2026-10-08. Provider billing remains the
# source of truth.
MODEL_PRICING_USD_PER_MILLION = {
    "gpt-5.6-sol": {"input": Decimal("5.00"), "cached_input": Decimal("0.50"), "output": Decimal("30.00")},
    "gpt-5.6-terra": {"input": Decimal("2.50"), "cached_input": Decimal("0.25"), "output": Decimal("15.00")},
    "gpt-5.6-luna": {"input": Decimal("1.00"), "cached_input": Decimal("0.10"), "output": Decimal("6.00")},
    # Yandex AI Studio synchronous prices, without VAT (aistudio.yandex.ru pricing page).
    # YandexGPT has no cache discount; DeepSeek cached input has a separate lower rate.
    "yandexgpt-5.1": {"input": Decimal("6.557376"), "cached_input": Decimal("6.557376"), "output": Decimal("6.557376")},
    "yandexgpt-5-lite": {"input": Decimal("1.639344"), "cached_input": Decimal("1.639344"), "output": Decimal("1.639344")},
    "deepseek-v4-flash": {"input": Decimal("2.459016"), "cached_input": Decimal("0.614754"), "output": Decimal("4.09836")},
    "deepseek-v4.1-flash": {"input": Decimal("2.459016"), "cached_input": Decimal("0.614754"), "output": Decimal("4.09836")},
    "qwen3.6-35b-a3b": {"input": Decimal("1.639344"), "cached_input": Decimal("0.409836"), "output": Decimal("2.459016")},
    "gpt-oss-120b": {"input": Decimal("2.459016"), "cached_input": Decimal("2.459016"), "output": Decimal("2.459016")},
}

LONG_CONTEXT_THRESHOLD = 272_000


def pricing_for_model(model: str) -> tuple[str | None, dict[str, Decimal] | None]:
    """Return a catalog key and rates for aliases/snapshots of a known model."""
    normalized = str(model or "").strip().lower()
    if normalized == "gpt-5.6":
        return "gpt-5.6-sol", MODEL_PRICING_USD_PER_MILLION["gpt-5.6-sol"]
    for key, rates in MODEL_PRICING_USD_PER_MILLION.items():
        if normalized == key or normalized.startswith(f"{key}-"):
            return key, rates
    return None, None


def operation_from_payload(payload: dict) -> str:
    """Classify the call without adding private fields to the provider request."""
    text_format = payload.get("text", {}).get("format", {})
    format_name = str(text_format.get("name", ""))
    if format_name == "route_decision":
        return "routing"
    if format_name == "council_opinion":
        return "council_opinion"
    if format_name == "agent_result":
        return "agent_response"
    if format_name == "health_passport":
        return "health_passport"
    instructions = str(payload.get("instructions", "")).casefold()
    if "ведущий консилиума" in instructions:
        return "council_summary"
    input_value = payload.get("input", [])
    if isinstance(input_value, list):
        for message in input_value:
            for content in message.get("content", []) if isinstance(message, dict) else []:
                if isinstance(content, dict) and content.get("type") == "input_file":
                    return "lab_interpretation"
    return "other"


def cost_fields_for_usage(
    model: str, input_tokens: int, cached_tokens: int,
    output_tokens: int, long_context: bool,
) -> dict:
    """Calculate the persisted rate snapshot for current and migrated usage."""
    pricing_key, rates = pricing_for_model(model)
    cached_tokens = min(max(0, int(input_tokens)), max(0, int(cached_tokens)))
    uncached_tokens = max(0, int(input_tokens) - cached_tokens)
    output_tokens = max(0, int(output_tokens))
    input_cost = cached_cost = output_cost = Decimal("0")
    if rates:
        input_multiplier = Decimal("2") if long_context else Decimal("1")
        output_multiplier = Decimal("1.5") if long_context else Decimal("1")
        million = Decimal("1000000")
        input_cost = Decimal(uncached_tokens) * rates["input"] * input_multiplier / million
        cached_cost = Decimal(cached_tokens) * rates["cached_input"] * input_multiplier / million
        output_cost = Decimal(output_tokens) * rates["output"] * output_multiplier / million

    quantize = lambda value: float(value.quantize(Decimal("0.000000001"), rounding=ROUND_HALF_UP))
    return {
        "pricing_key": pricing_key or "",
        "pricing_known": bool(rates),
        "input_rate": float(rates["input"]) if rates else 0.0,
        "cached_input_rate": float(rates["cached_input"]) if rates else 0.0,
        "output_rate": float(rates["output"]) if rates else 0.0,
        "input_cost_usd": quantize(input_cost),
        "cached_input_cost_usd": quantize(cached_cost),
        "output_cost_usd": quantize(output_cost),
        "total_cost_usd": quantize(input_cost + cached_cost + output_cost),
    }


def usage_record(response: dict, payload: dict, chel_id: str = "") -> dict | None:
    """Build a privacy-safe storage record from a Responses API response."""
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None

    def nonnegative_int(value) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    input_tokens = nonnegative_int(usage.get("input_tokens"))
    cached_tokens = min(
        input_tokens,
        nonnegative_int((usage.get("input_tokens_details") or {}).get("cached_tokens")),
    )
    output_tokens = nonnegative_int(usage.get("output_tokens"))
    reasoning_tokens = min(
        output_tokens,
        nonnegative_int((usage.get("output_tokens_details") or {}).get("reasoning_tokens")),
    )
    total_tokens = nonnegative_int(usage.get("total_tokens")) or input_tokens + output_tokens
    model = str(response.get("model") or payload.get("model") or "unknown")[:120]
    long_context = input_tokens > LONG_CONTEXT_THRESHOLD
    costs = cost_fields_for_usage(
        model, input_tokens, cached_tokens, output_tokens, long_context,
    )
    return {
        "chel_id": str(chel_id or "")[:80],
        "operation": operation_from_payload(payload),
        "model": model,
        "long_context": long_context,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
        **costs,
    }


def public_pricing_catalog() -> list[dict]:
    return [
        {
            "model": model,
            "input": float(rates["input"]),
            "cached_input": float(rates["cached_input"]),
            "output": float(rates["output"]),
        }
        for model, rates in MODEL_PRICING_USD_PER_MILLION.items()
    ]
