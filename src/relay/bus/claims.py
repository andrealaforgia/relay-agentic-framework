"""Crash recovery over the PEL: steal stale pending entries from dead
consumers of the same group (how a standby worker on another machine takes
over a role), and inspect delivery counts (how the delivery cap is enforced)."""

from __future__ import annotations

import redis

from relay.bus.groups import Delivery
from relay.contract.envelope import Envelope


def autoclaim_stale(
    client: redis.Redis,
    stream: str,
    group: str,
    consumer: str,
    min_idle_ms: int = 300_000,
    count: int = 64,
) -> list[Delivery]:
    _cursor, entries, _deleted = client.xautoclaim(
        stream, group, consumer, min_idle_time=min_idle_ms, start_id="0-0", count=count
    )
    return [
        Delivery(stream_id=stream_id, envelope=Envelope.try_from_fields(fields), raw=fields)
        for stream_id, fields in entries
        if fields
    ]


def delivery_count(client: redis.Redis, stream: str, group: str, stream_id: str) -> int:
    entries = client.xpending_range(stream, group, min=stream_id, max=stream_id, count=1)
    if not entries:
        return 0
    return int(entries[0]["times_delivered"])
