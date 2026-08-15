"""What the engagement cost, folded from the ledger.

`usage.reported` events carry one model turn each, attached to the work item
by the envelope refs. Everything here is arithmetic over that stream: no
second source, no state document, and nothing that dies with the session
transcripts (D3).

The columns that answer the questions worth asking:
  cache read vs cache write  — are sessions staying warm, or is every turn
                               paying full price for context it already had?
  fresh sessions             — how often rotation made us start cold
  agent turns                — how much rediscovery each invocation did
  model                      — the tier that actually billed, per role
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import redis

from relay.ledger.reader import read_all

COUNTERS = ("input_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens", "output_tokens", "agent_turns")
# Anthropic's multipliers on the base input price: a cache write costs 1.25x,
# a cache read a tenth. Comparing raw token counts across a rotation change
# would otherwise flatter whichever option writes more and reads less.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1


def _blank() -> dict[str, Any]:
    row: dict[str, Any] = {k: 0 for k in COUNTERS}
    row["turns"] = 0
    row["fresh_sessions"] = 0
    row["cost_usd"] = 0.0
    row["duration_s"] = 0.0
    return row


def _accumulate(row: dict[str, Any], payload: dict[str, Any]) -> None:
    row["turns"] += 1
    row["fresh_sessions"] += 1 if payload.get("fresh_session") else 0
    row["cost_usd"] += float(payload.get("cost_usd") or 0.0)
    row["duration_s"] += float(payload.get("duration_s") or 0.0)
    for key in COUNTERS:
        row[key] += int(payload.get(key) or 0)


def billed_input_equivalents(row: dict[str, Any]) -> float:
    """Input tokens weighted by what they actually cost.

    Cache reads are a tenth of the price and cache writes a quarter dearer, so
    the raw token total says almost nothing about the bill. This is the number
    to compare two runs on.
    """
    return float(
        row["input_tokens"]
        + row["cache_creation_input_tokens"] * CACHE_WRITE_MULTIPLIER
        + row["cache_read_input_tokens"] * CACHE_READ_MULTIPLIER
    )


@dataclass
class UsageReport:
    by_role: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_behaviour: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_model: dict[str, dict[str, Any]] = field(default_factory=dict)
    total: dict[str, Any] = field(default_factory=_blank)

    @property
    def empty(self) -> bool:
        return int(self.total["turns"]) == 0


def fold_usage(client: redis.Redis, swarm: str) -> UsageReport:
    by_role: dict[str, dict[str, Any]] = defaultdict(_blank)
    by_behaviour: dict[str, dict[str, Any]] = defaultdict(_blank)
    by_model: dict[str, dict[str, Any]] = defaultdict(_blank)
    total = _blank()

    for _sid, env in read_all(client, swarm):
        if env.type != "usage.reported":
            continue
        payload = env.payload
        role = str(payload.get("role") or env.from_role)
        _accumulate(by_role[role], payload)
        _accumulate(by_model[str(payload.get("model") or "unknown")], payload)
        _accumulate(total, payload)
        # work-item attribution: the gate that has no behaviour is still work
        subject = env.behaviour_id or env.iteration_id or env.gate_id
        if subject:
            _accumulate(by_behaviour[subject], payload)

    return UsageReport(dict(by_role), dict(by_behaviour), dict(by_model), total)
