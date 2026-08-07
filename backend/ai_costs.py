"""Token usage parsing and estimated OpenAI API cost calculation.

The database stores the rate snapshot used for every request so historical totals do
not change when the price catalog is updated.  No prompt or response text is stored.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


# USD per one million text tokens, verified against the official model pages on
# 2026-08-03.  The final OpenAI invoice remains the source of truth.
MODEL_PRICING_USD_PER_MILLION = {
    "gpt-5.6-sol": {"input": Decimal("5.00"), "cached_input": Decimal("0.50"), "output": Decimal("30.00")},
    "gpt-5.6-terra": {"input": Decimal("2.50"), "cached_input": Decimal("0.25"), "output": Decimal("15.00")},
    "gpt-5.6-luna": {"input": Decimal("1.00"), "cached_input": Decimal("0.10"), "output": Decimal("6.00")},
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
    pricing_key, rates = pricing_for_model(model)
    long_context = input_tokens > LONG_CONTEXT_THRESHOLD

    uncached_tokens = max(0, input_tokens - cached_tokens)
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
        "chel_id": str(chel_id or "")[:80],
        "operation": operation_from_payload(payload),
        "model": model,
        "pricing_key": pricing_key or "",
        "pricing_known": bool(rates),
        "long_context": long_context,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
        "input_rate": float(rates["input"]) if rates else 0.0,
        "cached_input_rate": float(rates["cached_input"]) if rates else 0.0,
        "output_rate": float(rates["output"]) if rates else 0.0,
        "input_cost_usd": quantize(input_cost),
        "cached_input_cost_usd": quantize(cached_cost),
        "output_cost_usd": quantize(output_cost),
        "total_cost_usd": quantize(input_cost + cached_cost + output_cost),
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
