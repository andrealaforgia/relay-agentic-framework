"""The sentinel: audits that every message stays within its author's realm.

Two layers, cheapest first:
  1. MECHANICAL (code, deterministic): sequence gaps; verdicts citing run ids
     that never completed on the ledger; gate verdicts for gates never
     requested. These publish control.correction without any model.
  2. SEMANTIC (LLM, batched): the provenance test — did the sender choose
     that content, or did the problem dictate it? A builder re-interpreting
     requirements, a reviewer smuggling design decisions into findings, an
     interpreter leaking implementation detail to the owner.

The sentinel never blocks and never rewrites: it corrects (control plane,
same stream, fully audited) and escalates repeat offenders to the
interpreter. Ignoring it is not sustainable: escalation reaches the owner.
"""

from __future__ import annotations

import json
import time

import redis

from relay.bus import groups
from relay.contract.envelope import Envelope
from relay.ledger.reader import read_all
from relay.runners.base import Runner
from relay.workers.base import Worker, _now
from pathlib import Path

from ulid import ULID

BATCH_SIZE = 12          # audit the semantic layer in batches, not per event
BATCH_MAX_AGE_S = 120.0
ACK_TIMEOUT_S = 600.0
STRIKE_LIMIT = 3
AUDITED_PLANES = ("chat", "plan", "work", "gate")
TURN_TIMEOUT_S = 600


def _new_finding_id() -> str:
    return f"find-{ULID()}"


from dataclasses import dataclass  # noqa: E402


@dataclass
class CorrectionRecord:
    role: str
    ts: float
    acked: bool = False


class SentinelWorker(Worker):
    def __init__(
        self,
        swarm: str,
        runner: Runner,
        playbook_path: Path,
        workspace: Path,
        state_dir: Path,
        client: redis.Redis | None = None,
    ) -> None:
        super().__init__(swarm, "sentinel", client=client)
        self.runner = runner
        self.playbook = playbook_path.read_text()
        self.workspace = workspace
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._session_file = state_dir / "session.txt"
        # mechanical state, rebuilt from the ledger on start
        self.completed_runs: set[str] = set()
        self.requested_gates: set[str] = set()
        self.expected_seq = 1
        self.corrected: set[tuple[str, str]] = set()          # (subject_event_id, rule)
        self.corrections_out: dict[str, CorrectionRecord] = {}
        self.escalated_roles: set[str] = set()
        self.batch: list[Envelope] = []
        self._batch_started = 0.0

    # the sentinel reads everything; nothing is addressed to it except acks
    def wants(self, env: Envelope) -> bool:
        return True

    # ── bootstrap: rebuild mechanical state, audit nothing retroactively ─────

    def start(self) -> None:
        groups.ensure_group(self.client, self.stream, self.group)
        self.announce_started()
        self.heartbeat()
        for _sid, env in read_all(self.client, self.swarm):
            self._absorb_silently(env)
        # everything replayed is already absorbed: clear our PEL backlog
        for delivery in groups.read_pending(self.client, self.stream, self.group, self.consumer):
            groups.ack(self.client, self.stream, self.group, delivery.stream_id)
        self.expected_seq = max(self.expected_seq, 1)

    def _absorb_silently(self, env: Envelope) -> None:
        if env.seq is not None:
            self.expected_seq = max(self.expected_seq, env.seq + 1)
        if env.type == "run.completed":
            self.completed_runs.add(str(env.payload.get("run_id")))
        elif env.type == "gate.requested":
            self.requested_gates.add(str(env.payload.get("gate_id")))
        elif env.type == "control.correction" and env.from_role == "sentinel":
            self.corrected.add((str(env.payload.get("subject_event_id")),
                                str(env.payload.get("rule_id"))))
            self.corrections_out[str(env.payload.get("finding_id"))] = CorrectionRecord(
                role=env.to_role, ts=time.time()
            )
        elif env.type == "control.ack":
            finding = self.corrections_out.get(str(env.payload.get("finding_id")))
            if finding:
                finding.acked = True
        elif env.type == "sentinel.escalation" and env.from_role == "sentinel":
            self.escalated_roles.add(str(env.payload.get("role")))

    # ── live processing ──────────────────────────────────────────────────────

    def _process(self, delivery: groups.Delivery) -> None:
        env = delivery.envelope
        groups.ack(self.client, self.stream, self.group, delivery.stream_id)
        if env.from_role == "sentinel":
            self._absorb_silently(env)
            return
        self._check_sequence(env)
        self._absorb_silently(env)
        self._check_mechanical(env)
        if (
            env.plane in AUDITED_PLANES
            and env.from_role not in ("owner", "coordinator", "toolgate")
        ):
            if not self.batch:
                self._batch_started = time.monotonic()
            self.batch.append(env)

    def _check_sequence(self, env: Envelope) -> None:
        if env.seq is not None and env.seq > self.expected_seq:
            self.publisher.send(
                "sentinel", "system", "system.gap_detected",
                {"expected_seq": self.expected_seq, "observed_seq": env.seq},
            )

    def _check_mechanical(self, env: Envelope) -> None:
        if env.type == "work.acceptance_verdict":
            run_id = str(env.payload.get("run_id"))
            if run_id not in self.completed_runs:
                self._correct(env, "evidence.run-not-on-ledger", "retract",
                              f"verdict cites {run_id}, but no such run.completed exists")
        elif env.type == "gate.verdict":
            gate_id = str(env.payload.get("gate_id"))
            if gate_id not in self.requested_gates:
                self._correct(env, "gate.never-requested", "retract",
                              f"verdict for {gate_id}, but no gate.requested exists")

    def _correct(self, env: Envelope, rule: str, remedy: str, note: str) -> None:
        if (env.event_id, rule) in self.corrected:
            return
        finding_id = _new_finding_id()
        self.publisher.send(
            "sentinel", env.from_role, "control.correction",
            {
                "finding_id": finding_id,
                "subject_event_id": env.event_id,
                "rule_id": rule,
                "required_remedy": remedy,
                "note": note[:500],
            },
            in_reply_to=env.event_id,
        )
        self.corrected.add((env.event_id, rule))
        self.corrections_out[finding_id] = CorrectionRecord(role=env.from_role, ts=time.time())
        print(f"[{_now()}] correction -> {env.from_role}: {rule}", flush=True)

    # ── batched semantic audit + escalation, on the worker's own clock ──────

    def on_tick(self) -> None:
        if self.batch and (
            len(self.batch) >= BATCH_SIZE
            or time.monotonic() - self._batch_started > BATCH_MAX_AGE_S
        ):
            batch, self.batch = self.batch, []
            self._semantic_audit(batch)
        self._escalate_if_needed()

    def _semantic_audit(self, batch: list[Envelope]) -> None:
        listing = "\n".join(
            f"- seq={e.seq} event_id={e.event_id} edge={e.from_role}>{e.to_role} "
            f"type={e.type}\n  payload: {json.dumps(e.payload, sort_keys=True)[:600]}"
            for e in batch
        )
        prompt = (
            f"{self.playbook}\n\n== Messages to audit ==\n{listing}\n\n"
            f"For each violation, publish ONE correction:\n"
            f"  relay-send --swarm {self.swarm} --from sentinel --to <culprit role> "
            f"--type control.correction --reply-to <event_id> --payload "
            f"'{{\"finding_id\": \"'$(relay-id find)'\", \"subject_event_id\": \"<event_id>\", "
            f"\"rule_id\": \"<rule>\", \"required_remedy\": \"resend_on_contract|retract|"
            f"acknowledge_rule\", \"note\": \"<why, max 500 chars>\"}}'\n"
            f"If nothing violates its author's realm, publish nothing and reply 'clean'."
        )
        self.heartbeat(status=f"auditing {len(batch)} messages")
        result = self.runner.run_turn(
            prompt=prompt, cwd=self.workspace,
            session_ref=self._session_ref(), timeout_s=TURN_TIMEOUT_S,
            on_event=lambda a: print(f"[{_now()}]   {a}", flush=True),
        )
        if result.session_ref:
            self._session_file.write_text(result.session_ref)
        self.heartbeat(status="idle")

    def _session_ref(self) -> str | None:
        if self._session_file.exists():
            return self._session_file.read_text().strip() or None
        return None

    def _escalate_if_needed(self) -> None:
        by_role: dict[str, list[tuple[str, CorrectionRecord]]] = {}
        for finding_id, info in self.corrections_out.items():
            by_role.setdefault(info.role, []).append((finding_id, info))
        for role, findings in by_role.items():
            if role in self.escalated_roles:
                continue
            unacked_overdue = [
                fid for fid, info in findings
                if not info.acked and time.time() - info.ts > ACK_TIMEOUT_S
            ]
            if len(findings) >= STRIKE_LIMIT or unacked_overdue:
                reason = (
                    f"{len(findings)} corrections issued to {role}"
                    + (f"; {len(unacked_overdue)} unacknowledged past "
                       f"{int(ACK_TIMEOUT_S)}s" if unacked_overdue else "")
                )
                self.publisher.send(
                    "sentinel", "interpreter", "sentinel.escalation",
                    {"role": role, "finding_ids": [fid for fid, _ in findings][:20],
                     "reason": reason},
                )
                self.escalated_roles.add(role)
