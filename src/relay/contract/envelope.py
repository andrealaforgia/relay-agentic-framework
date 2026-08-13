"""The message envelope: the flat field map every ledger entry is made of.

Redis streams store flat string maps, so the envelope round-trips to/from
`dict[str, str]` (`to_fields` / `from_fields`). The JSON `payload` is the only
nested part and is carried as a JSON string in the `payload` field.
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from ulid import ULID

ULID_PATTERN = r"^[0-9A-HJKMNP-TV-Z]{26}$"

# Optional envelope-level correlation/reference fields. Empty string on the
# wire means "not applicable"; None in the model.
_OPTIONAL_FIELDS = ("in_reply_to", "iteration_id", "story_id", "behaviour_id", "gate_id", "commit_sha")


def new_event_id() -> str:
    return str(ULID())


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Envelope(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    event_id: str = Field(pattern=ULID_PATTERN, default_factory=new_event_id)
    seq: int | None = None  # assigned atomically at publish time
    swarm: str = Field(min_length=1)
    plane: str = Field(min_length=1)
    from_role: str = Field(alias="from", min_length=1)
    to_role: str = Field(alias="to", min_length=1)
    type: str = Field(min_length=1)
    in_reply_to: str | None = Field(default=None, pattern=ULID_PATTERN)
    iteration_id: str | None = None
    story_id: str | None = None
    behaviour_id: str | None = None
    gate_id: str | None = None
    commit_sha: str | None = None
    contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    producer_host: str = Field(default_factory=socket.gethostname)
    producer_pid: int = 0
    ts: str = Field(default_factory=now_iso)
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_fields(self) -> dict[str, str]:
        """Flat string map for XADD. None -> empty string."""
        fields: dict[str, str] = {
            "event_id": self.event_id,
            "seq": "" if self.seq is None else str(self.seq),
            "swarm": self.swarm,
            "plane": self.plane,
            "from": self.from_role,
            "to": self.to_role,
            "type": self.type,
            "contract_hash": self.contract_hash,
            "producer_host": self.producer_host,
            "producer_pid": str(self.producer_pid),
            "ts": self.ts,
            "payload": json.dumps(self.payload, sort_keys=True),
        }
        for name in _OPTIONAL_FIELDS:
            value = getattr(self, name)
            fields[name] = value if value is not None else ""
        return fields

    @classmethod
    def from_fields(cls, fields: dict[str, str]) -> "Envelope":
        data: dict[str, Any] = dict(fields)
        data["payload"] = json.loads(data.get("payload") or "{}")
        seq = data.get("seq")
        data["seq"] = int(seq) if seq not in (None, "") else None
        data["producer_pid"] = int(data.get("producer_pid") or 0)
        for name in _OPTIONAL_FIELDS:
            if data.get(name) == "":
                data[name] = None
        return cls.model_validate(data)
