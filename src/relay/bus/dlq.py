"""Dead letters. Nothing is ever dropped silently: routing to the DLQ also
publishes a message.quarantined event on the ledger so the audit trail records
that (and why) something was set aside."""

from __future__ import annotations

import json

import redis

from relay.bus.keys import dlq_key
from relay.bus.publisher import Publisher

DLQ_REASONS = ("off_contract", "delivery_cap_exceeded", "contract_drift",
               "unparseable", "superseded")


def route_to_dlq(
    client: redis.Redis,
    publisher: Publisher,
    swarm: str,
    routed_by: str,
    reason: str,
    raw_fields: dict[str, str],
    detail: str = "",
) -> str:
    if reason not in DLQ_REASONS:
        raise ValueError(f"unknown DLQ reason: {reason}")
    dlq_id = client.xadd(
        dlq_key(swarm),
        {
            "original_event_id": raw_fields.get("event_id", ""),
            "reason": reason,
            "detail": detail,
            "routed_by": routed_by,
            "raw": json.dumps(raw_fields, sort_keys=True),
        },
    )
    payload: dict[str, object] = {"reason": reason, "detail": detail}
    original = raw_fields.get("event_id", "")
    if original:
        payload["original_event_id"] = original
    publisher.send(routed_by, "system", "message.quarantined", payload)
    return str(dlq_id)


def dlq_depth(client: redis.Redis, swarm: str) -> int:
    return int(client.xlen(dlq_key(swarm)))
