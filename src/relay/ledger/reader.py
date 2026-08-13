"""Read the whole ledger (no consumer group — a pure, side-effect-free scan).

Used by replay/audit/export and by every viewer. Reading never mutates
anything: viewers and audits can run against a live swarm freely.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import redis

from relay.bus.keys import ledger_key
from relay.contract.envelope import Envelope


def read_all_raw(client: redis.Redis, swarm: str, batch: int = 512) -> Iterator[tuple[str, dict[str, str]]]:
    """Yield (stream_id, raw fields) in stream order — including entries that
    are not valid v2 envelopes (foreign writers, corruption)."""
    last = "-"
    while True:
        entries = cast(
            "list[tuple[str, dict[str, str]]]",
            client.xrange(ledger_key(swarm), min=last, max="+", count=batch),
        )
        if last != "-":
            entries = entries[1:]  # xrange min is inclusive
        if not entries:
            return
        yield from entries
        last = entries[-1][0]


def read_all(client: redis.Redis, swarm: str, batch: int = 512) -> Iterator[tuple[str, Envelope]]:
    """Yield (stream_id, envelope) in stream order, skipping entries that are
    not valid v2 envelopes. Use read_all_raw + Envelope.try_from_fields to see
    (and report on) the skipped ones — the audit does."""
    for stream_id, fields in read_all_raw(client, swarm, batch=batch):
        env = Envelope.try_from_fields(fields)
        if env is not None:
            yield stream_id, env
