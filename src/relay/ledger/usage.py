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

from relay.contract.envelope import Envelope
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
    # which tiers actually billed for this row: a role that quietly ran on
    # the priciest model is the incident this makes visible at a glance
    row["models"] = set()
    return row


def _accumulate(row: dict[str, Any], payload: dict[str, Any]) -> None:
    row["turns"] += 1
    row["models"].add(str(payload.get("model") or "unknown"))
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


class UsageFold:
    """The running total, one event at a time.

    The report and the live view share this so they can never disagree about
    what a swarm has spent.
    """

    def __init__(self) -> None:
        self.by_role: dict[str, dict[str, Any]] = defaultdict(_blank)
        self.by_behaviour: dict[str, dict[str, Any]] = defaultdict(_blank)
        self.by_model: dict[str, dict[str, Any]] = defaultdict(_blank)
        self.total: dict[str, Any] = _blank()

    def add(self, env: Envelope) -> bool:
        """Fold one event in. False if it was not a usage event."""
        if env.type != "usage.reported":
            return False
        payload = env.payload
        self._accumulate_all(payload, str(payload.get("role") or env.from_role),
                             env.behaviour_id or env.iteration_id or env.gate_id)
        return True

    def _accumulate_all(self, payload: dict[str, Any], role: str, subject: str | None) -> None:
        _accumulate(self.by_role[role], payload)
        _accumulate(self.by_model[str(payload.get("model") or "unknown")], payload)
        _accumulate(self.total, payload)
        # work-item attribution: the gate that has no behaviour is still work
        if subject:
            _accumulate(self.by_behaviour[subject], payload)

    def report(self) -> UsageReport:
        return UsageReport(dict(self.by_role), dict(self.by_behaviour),
                           dict(self.by_model), self.total)


def fold_usage(client: redis.Redis, swarm: str) -> UsageReport:
    fold = UsageFold()
    for _sid, env in read_all(client, swarm):
        fold.add(env)
    return fold.report()
