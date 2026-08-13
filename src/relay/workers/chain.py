"""The generic LLM worker: one class for every assistant role.

Per triggering message: build the prompt (playbook + trigger + protocol
reminder) -> invoke the runner -> VERIFY a reply from this role with
in_reply_to = trigger is actually on the ledger (never trust the model's
say-so) -> corrective re-prompt up to MAX_CORRECTIONS times -> DLQ + loud
system.worker_error if the model never delivers.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import cast

import redis

from relay.bus import dlq
from relay.bus.keys import ledger_key
from relay.contract.envelope import Envelope
from relay.runners.base import Runner
from relay.workers.base import Worker

MAX_CORRECTIONS = 2
TURN_TIMEOUT_S = 1800
VERIFY_SCAN_COUNT = 500

PROTOCOL_REMINDER = """\
== Relay protocol (mechanical rules, enforced in code) ==
- You are the '{role}' assistant of swarm '{swarm}'.
- Your ONLY output channel is the relay-send CLI. Plain text you print is logged but is NOT work product.
- Reply to the message below by running:
    relay-send --swarm {swarm} --from {role} --to <recipient> --type <type> \\
      --reply-to {event_id} --payload '<json>'
- relay-send validates the edge, type and payload; if it prints an error to stderr, fix the payload and retry within this same turn.
- You may have partially handled this message before a restart: check the current state (git log, existing files, the error text) before redoing work.

== Triggering message ==
from: {from_role}    type: {type}    event_id: {event_id}
payload:
{payload}
"""


class ChainWorker(Worker):
    def __init__(
        self,
        swarm: str,
        role: str,
        runner: Runner,
        playbook_path: Path,
        workspace: Path,
        state_dir: Path,
        client: redis.Redis | None = None,
    ) -> None:
        super().__init__(swarm, role, client=client)
        self.runner = runner
        self.playbook = playbook_path.read_text()
        self.workspace = workspace
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._session_file = state_dir / "session.txt"

    # ── session persistence ──────────────────────────────────────────────────

    def _session_ref(self) -> str | None:
        if self._session_file.exists():
            return self._session_file.read_text().strip() or None
        return None

    def _save_session(self, ref: str | None) -> None:
        previous = self._session_ref()
        if ref and ref != previous:
            self._session_file.write_text(ref)
            self.publisher.send(
                self.role, "system", "system.runner_session_started",
                {"role": self.role, "session_ref": ref},
            )

    # ── the verify-don't-trust core ──────────────────────────────────────────

    def _reply_on_stream(self, trigger_event_id: str) -> str | None:
        """Find a message FROM this role with in_reply_to = trigger. Exact
        typed match — the v1 'any newer own message' heuristic is gone."""
        entries = cast(
            "list[tuple[str, dict[str, str]]]",
            self.client.xrevrange(ledger_key(self.swarm), count=VERIFY_SCAN_COUNT),
        )
        for _sid, fields in entries:
            if fields.get("from") == self.role and fields.get("in_reply_to") == trigger_event_id:
                return fields.get("event_id")
        return None

    def handle(self, env: Envelope) -> str | None:
        # Crashed after publishing, before acking? The reply is already on the
        # stream — never invoke the model again for work that is already done.
        existing = self._reply_on_stream(env.event_id)
        if existing is not None:
            return existing

        base_prompt = self.playbook + "\n\n" + PROTOCOL_REMINDER.format(
            role=self.role,
            swarm=self.swarm,
            event_id=env.event_id,
            from_role=env.from_role,
            type=env.type,
            payload=json.dumps(env.payload, indent=2, sort_keys=True),
        )
        prompt = base_prompt

        def on_event(activity: str) -> None:
            print(f"[{time.strftime('%H:%M:%S')}]   {activity}", flush=True)
            self.heartbeat(status=f"{env.type}: {activity[:120]}")

        for _correction in range(MAX_CORRECTIONS + 1):
            result = self.runner.run_turn(
                prompt=prompt,
                cwd=self.workspace,
                session_ref=self._session_ref(),
                timeout_s=TURN_TIMEOUT_S,
                on_event=on_event,
            )
            self._save_session(result.session_ref)

            reply_id = self._reply_on_stream(env.event_id)
            if reply_id is not None:
                return reply_id

            detail = result.error or "turn ended with no reply on the stream"
            # append, never replace: a runner without session resume must
            # still see the original trigger it is being corrected about
            prompt = base_prompt + (
                f"\n\n== Correction ==\n"
                f"Your previous turn published no reply to event {env.event_id} "
                f"({detail}). Nothing counts until it is on the stream. "
                f"Check what already exists (git log, files) before redoing work, "
                f"then run relay-send with --reply-to {env.event_id}."
            )

        # the model never delivered: loud failure, never a silent stall
        self.publisher.send(
            self.role, "system", "system.worker_error",
            {"role": self.role, "kind": "runner_failure",
             "detail": f"no reply published for {env.event_id} after {MAX_CORRECTIONS + 1} turns"},
        )
        dlq.route_to_dlq(
            self.client, self.publisher, self.swarm, self.role,
            "unparseable", env.to_fields(),
            f"model produced no on-stream reply after {MAX_CORRECTIONS + 1} attempts",
        )
        return None
