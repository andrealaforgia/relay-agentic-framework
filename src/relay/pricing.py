"""What a turn cost, when the runner did not say.

Worker turns get `total_cost_usd` from the runner. The Interpreter does not:
its spend is only visible in a Claude Code transcript, which records tokens
and no price. Without this the priciest, least-bounded session in the swarm
reports $0.00 — the one number guaranteed to be wrong.

Rates are per million tokens. Cache reads bill at a tenth; cache writes bill
at 2x, not 1.25x, because Claude Code uses the one-hour cache TTL (measured:
a turn's reported cost reconciles exactly at 2x and not at 1.25x).

An estimate is labelled as one. `relay costs` reports runner-reported cost
and estimates in the same column because the alternative — a zero — is a
worse lie than an approximation.
"""

from __future__ import annotations

CACHE_WRITE_MULTIPLIER = 2.0     # 1h TTL, which is what Claude Code writes
CACHE_READ_MULTIPLIER = 0.1

# (input, output) per million tokens
RATES: dict[str, tuple[float, float]] = {
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
    "fable": (10.0, 50.0),
}
UNKNOWN_RATE = (3.0, 15.0)       # assume Sonnet rather than assume free


def rate_for(model: str) -> tuple[float, float]:
    """Match on tier, not on exact id: ids carry dates and suffixes."""
    name = model.lower()
    for tier, rate in RATES.items():
        if tier in name:
            return rate
    return UNKNOWN_RATE


def estimate_cost(model: str, usage: dict[str, int]) -> float:
    """Dollars for one turn's token counts. Never negative, never a guess at
    tokens — only at the price of tokens we actually counted."""
    input_rate, output_rate = rate_for(model)
    billed_input = (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0) * CACHE_WRITE_MULTIPLIER
        + int(usage.get("cache_read_input_tokens") or 0) * CACHE_READ_MULTIPLIER
    )
    output = int(usage.get("output_tokens") or 0)
    return billed_input / 1_000_000 * input_rate + output / 1_000_000 * output_rate
