"""Consumer-group lifecycle and reads.

Groups are created idempotently at id 0 so a role added later still sees the
full history. Reads come in two flavours: `read_pending` (own PEL — always
drained first on startup) and `read_new` (blocking on '>').
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import redis

from relay.contract.envelope import Envelope


@dataclass(frozen=True)
class Delivery:
    """envelope is None for entries that don't parse as v2 envelopes (foreign
    writers on the same stream, corruption). raw always carries the fields so
    the consumer can ack, warn, or dead-letter — never crash."""

    stream_id: str
    envelope: Envelope | None
    raw: dict[str, str]


def ensure_group(client: redis.Redis, stream: str, group: str) -> None:
    try:
        client.xgroup_create(stream, group, id="0", mkstream=True)
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


def _to_deliveries(reply: object) -> list[Delivery]:
    deliveries: list[Delivery] = []
    typed = cast("list[tuple[str, list[tuple[str, dict[str, str] | None]]]]", reply or [])
    for _stream, entries in typed:
        for stream_id, fields in entries:
            # fields can be None for entries deleted via XAUTOCLAIM edge cases
            if fields:
                deliveries.append(Delivery(
                    stream_id=stream_id,
                    envelope=Envelope.try_from_fields(fields),
                    raw=fields,
                ))
    return deliveries


def read_pending(
    client: redis.Redis, stream: str, group: str, consumer: str, count: int = 64
) -> list[Delivery]:
    reply = client.xreadgroup(group, consumer, {stream: "0"}, count=count)
    return _to_deliveries(reply)


def read_new(
    client: redis.Redis,
    stream: str,
    group: str,
    consumer: str,
    block_ms: int = 0,
    count: int = 16,
) -> list[Delivery]:
    reply = client.xreadgroup(group, consumer, {stream: ">"}, count=count, block=block_ms)
    return _to_deliveries(reply)


def ack(client: redis.Redis, stream: str, group: str, stream_id: str) -> None:
    client.xack(stream, group, stream_id)
