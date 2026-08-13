"""The one write path to the ledger.

Nothing else in the codebase (or in any playbook) may XADD to the ledger:
every publish goes validate-edge -> validate-payload -> plane check ->
contract-hash check -> atomic seq+XADD (seq.lua). A ContractError here means
the message never existed on the stream.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import redis

from relay.bus.keys import ledger_key, seq_key
from relay.contract.envelope import Envelope
from relay.contract.errors import PayloadViolation
from relay.contract.validator import ContractValidator

_SEQ_LUA = (Path(__file__).parent / "seq.lua").read_text()


@dataclass(frozen=True)
class PublishResult:
    event_id: str
    seq: int
    stream_id: str


class Publisher:
    def __init__(self, client: redis.Redis, validator: ContractValidator, swarm: str) -> None:
        self._client = client
        self._validator = validator
        self._swarm = swarm
        self._script = client.register_script(_SEQ_LUA)

    def publish(self, envelope: Envelope) -> PublishResult:
        self._validator.validate_message(
            envelope.from_role, envelope.to_role, envelope.type, envelope.payload
        )
        expected_plane = self._validator.plane_of(envelope.type)
        if envelope.plane != expected_plane:
            raise PayloadViolation(
                envelope.type,
                f"plane '{envelope.plane}' does not match contract plane '{expected_plane}'",
            )
        expected_hash = self._validator.contract.contract_hash
        if envelope.contract_hash != expected_hash:
            raise PayloadViolation(
                envelope.type,
                f"contract drift: envelope hash {envelope.contract_hash[:12]} != "
                f"loaded contract {expected_hash[:12]}",
            )
        if envelope.swarm != self._swarm:
            raise PayloadViolation(
                envelope.type, f"swarm '{envelope.swarm}' does not match publisher swarm '{self._swarm}'"
            )

        fields = envelope.to_fields()
        fields.pop("seq")  # assigned atomically by seq.lua
        flat: list[str] = []
        for key, value in fields.items():
            flat.extend((key, value))
        seq, stream_id = self._script(
            keys=[seq_key(self._swarm), ledger_key(self._swarm)], args=flat
        )
        return PublishResult(event_id=envelope.event_id, seq=int(seq), stream_id=stream_id)

    def send(
        self,
        from_role: str,
        to_role: str,
        type_: str,
        payload: dict[str, Any],
        *,
        in_reply_to: str | None = None,
        iteration_id: str | None = None,
        story_id: str | None = None,
        behaviour_id: str | None = None,
        gate_id: str | None = None,
        commit_sha: str | None = None,
    ) -> PublishResult:
        """Ergonomic path: builds the envelope with the correct plane and hash."""
        envelope = Envelope.model_validate(
            {
                "swarm": self._swarm,
                "plane": self._validator.plane_of(type_),
                "from": from_role,
                "to": to_role,
                "type": type_,
                "payload": payload,
                "in_reply_to": in_reply_to,
                "iteration_id": iteration_id,
                "story_id": story_id,
                "behaviour_id": behaviour_id,
                "gate_id": gate_id,
                "commit_sha": commit_sha,
                "contract_hash": self._validator.contract.contract_hash,
                "producer_pid": os.getpid(),
            }
        )
        return self.publish(envelope)
