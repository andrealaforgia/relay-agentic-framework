"""JSONL export/import.

Export makes ledger fixtures a one-liner from any live swarm (every real
incident becomes a replay test). Import exists for fixtures and for moving an
engagement between Redis instances — it bypasses the publisher's seq assignment
on purpose (the entries already carry their seq) but still refuses envelopes
that don't parse.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, cast

import redis

from relay.bus.keys import ledger_key, seq_key
from relay.contract.envelope import Envelope
from relay.ledger.reader import read_all


def export_jsonl(client: redis.Redis, swarm: str, path: Path) -> int:
    count = 0
    with path.open("w") as f:
        for _stream_id, env in read_all(client, swarm):
            f.write(json.dumps(env.to_fields(), sort_keys=True) + "\n")
            count += 1
    return count


def read_jsonl(path: Path) -> Iterator[Envelope]:
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                yield Envelope.from_fields(json.loads(line))


def import_envelopes(client: redis.Redis, swarm: str, envelopes: Iterable[Envelope]) -> int:
    """Load envelopes into an EMPTY swarm ledger, preserving their seq."""
    stream = ledger_key(swarm)
    if client.exists(stream):
        raise ValueError(f"refusing to import into non-empty ledger {stream}")
    max_seq = 0
    count = 0
    for env in envelopes:
        client.xadd(stream, cast("dict[Any, Any]", env.to_fields()))
        if env.seq is not None:
            max_seq = max(max_seq, env.seq)
        count += 1
    client.set(seq_key(swarm), max_seq)  # future publishes continue the numbering
    return count
