"""Re-validate an entire ledger against the contract.

`relay audit` replays every entry through the same checks the publisher
applies, plus the cross-entry checks only a full scan can do (seq contiguity,
in_reply_to referring to a real earlier event). A divergence is itself a
finding: the audit's job is to prove the ledger could have been produced by a
correctly-enforcing publisher — or show exactly where it couldn't.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import redis

from relay.contract.envelope import Envelope
from relay.contract.errors import ContractError
from relay.contract.validator import ContractValidator
from relay.ledger.reader import read_all


@dataclass
class AuditFinding:
    seq: int | None
    stream_id: str
    rule: str
    detail: str


@dataclass
class AuditReport:
    entries: int = 0
    findings: list[AuditFinding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings


def audit_ledger(client: redis.Redis, validator: ContractValidator, swarm: str) -> AuditReport:
    report = AuditReport()
    expected_seq = 1
    seen_event_ids: set[str] = set()
    contract_hashes_upgraded: set[str] = {validator.contract.contract_hash}

    def finding(env: Envelope, stream_id: str, rule: str, detail: str) -> None:
        report.findings.append(AuditFinding(env.seq, stream_id, rule, detail))

    for stream_id, env in read_all(client, swarm):
        report.entries += 1

        if env.seq != expected_seq:
            finding(env, stream_id, "seq_gap", f"expected seq {expected_seq}, observed {env.seq}")
            expected_seq = (env.seq or expected_seq) + 1
        else:
            expected_seq += 1

        try:
            validator.validate_message(env.from_role, env.to_role, env.type, env.payload)
            if env.plane != validator.plane_of(env.type):
                finding(env, stream_id, "plane_mismatch",
                        f"plane '{env.plane}' vs contract '{validator.plane_of(env.type)}'")
        except ContractError as e:
            finding(env, stream_id, "off_contract", str(e))

        if env.type == "system.contract_upgraded":
            contract_hashes_upgraded.add(str(env.payload.get("old_hash")))
        if env.contract_hash not in contract_hashes_upgraded:
            finding(env, stream_id, "contract_drift",
                    f"unknown contract hash {env.contract_hash[:12]}")

        if env.in_reply_to is not None and env.in_reply_to not in seen_event_ids:
            finding(env, stream_id, "dangling_reply",
                    f"in_reply_to {env.in_reply_to} does not reference an earlier event")

        if env.event_id in seen_event_ids:
            finding(env, stream_id, "duplicate_event_id", env.event_id)
        seen_event_ids.add(env.event_id)

    return report
