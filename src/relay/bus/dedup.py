"""The done-map: event_id -> result_event_id per role.

Checked before invoking the model for a triggering event; written after the
role's reply is verified on the stream. Makes duplicate delivery and
crash-after-publish-before-ack harmless (idempotency layer 1)."""

from __future__ import annotations

import redis

from relay.bus.keys import done_key


def already_done(client: redis.Redis, swarm: str, role: str, event_id: str) -> str | None:
    result = client.hget(done_key(swarm, role), event_id)
    return str(result) if result is not None else None


def mark_done(
    client: redis.Redis, swarm: str, role: str, event_id: str, result_event_id: str
) -> None:
    client.hset(done_key(swarm, role), event_id, result_event_id)
