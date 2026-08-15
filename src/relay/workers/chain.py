"""The generic LLM worker: one class for every assistant role.

Per triggering message: build the prompt (playbook + trigger + protocol
reminder) -> invoke the runner -> VERIFY a reply from this role with
in_reply_to = trigger is actually on the ledger (never trust the model's
say-so) -> corrective re-prompt up to MAX_CORRECTIONS times -> DLQ + loud
worker.failed if the model never delivers.
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
from relay.workers import briefing
from relay.workers.base import Worker

MAX_CORRECTIONS = 2
TURN_TIMEOUT_S = 1800
VERIFY_SCAN_COUNT = 500
# Sessions are a cost multiplier: every turn re-reads the whole history, so
# an unbounded --resume grows quadratically (observed: one 610-turn session
# re-reading ~220k tokens per call). Sessions are scoped to one work item and
# hard-capped; prompts are self-contained, so rotation never loses correctness.
MAX_SESSION_TURNS = 20

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
        self._session_meta_file = state_dir / "session_meta.json"
        self._cost_total = 0.0

    # ── session persistence ──────────────────────────────────────────────────

    def _session_ref(self) -> str | None:
        if self._session_file.exists():
            return self._session_file.read_text().strip() or None
        return None

    def _session_meta(self) -> dict[str, object]:
        if self._session_meta_file.exists():
            try:
                return dict(json.loads(self._session_meta_file.read_text()))
            except (json.JSONDecodeError, ValueError):
                pass
        return {"scope": "", "turns": 0}

    def _scoped_session(self, scope: str) -> str | None:
        """The session to resume — or None to start fresh when the work item
        changed or the session hit its turn cap (context stays bounded)."""
        meta = self._session_meta()
        if meta.get("scope") != scope:
            if self._session_ref():
                print(f"[{time.strftime('%H:%M:%S')}] fresh session: new work item {scope}",
                      flush=True)
            return None
        if int(str(meta.get("turns", 0))) >= MAX_SESSION_TURNS:
            print(f"[{time.strftime('%H:%M:%S')}] fresh session: {MAX_SESSION_TURNS}-turn "
                  f"cap reached for {scope}", flush=True)
            return None
        return self._session_ref()

    def _record_turn(self, scope: str, fresh: bool) -> None:
        meta = self._session_meta()
        turns = 1 if fresh or meta.get("scope") != scope else int(str(meta.get("turns", 0))) + 1
        self._session_meta_file.write_text(json.dumps({"scope": scope, "turns": turns}))

    def _save_session(self, ref: str | None) -> None:
        previous = self._session_ref()
        if ref and ref != previous:
            self._session_file.write_text(ref)
            self.publisher.send(
                self.role, "system", "session.started",
                {"role": self.role, "session_ref": ref},
            )

    # ── the verify-don't-trust core ──────────────────────────────────────────

    def _reply_on_stream(self, trigger_event_id: str) -> str | None:
        """Find a message FROM this role with in_reply_to = trigger. Exact
        typed match — the v1 'any newer own message' heuristic is gone.

        System-plane entries are never work product: the worker publishes its
        own bookkeeping (usage, failures) with the same lineage, and a silent
        model must never be able to pass verification on our paperwork.
        """
        entries = cast(
            "list[tuple[str, dict[str, str]]]",
            self.client.xrevrange(ledger_key(self.swarm), count=VERIFY_SCAN_COUNT),
        )
        for _sid, fields in entries:
            if fields.get("plane") == "system":
                continue
            if fields.get("from") == self.role and fields.get("in_reply_to") == trigger_event_id:
                return fields.get("event_id")
        return None

    def handle(self, env: Envelope) -> str | None:
        # Crashed after publishing, before acking? The reply is already on the
        # stream — never invoke the model again for work that is already done.
        existing = self._reply_on_stream(env.event_id)
        if existing is not None:
            return existing

        # Gate turns run in a detached worktree pinned to the reviewed SHA:
        # reviewing the wrong code is physically impossible.
        if env.type == "gate.requested" and env.payload.get("commit_sha"):
            return self._handle_in_pinned_worktree(env)

        return self._run_turn_loop(env, self.workspace)

    def _handle_in_pinned_worktree(self, env: Envelope) -> str | None:
        import tempfile

        from relay.gitops import branch as gitops

        sha = str(env.payload["commit_sha"])
        if not gitops.commit_exists(self.workspace, sha):
            self.publisher.send(
                self.role, "system", "worker.failed",
                {"role": self.role, "kind": "other",
                 "detail": f"gate {env.payload.get('gate_id')}: commit {sha} not present"},
            )
            return None
        with tempfile.TemporaryDirectory(prefix=f"relay-gate-{self.role}-") as tmp:
            worktree = Path(tmp) / "checkout"
            gitops.add_detached_worktree(self.workspace, sha, worktree)
            try:
                return self._run_turn_loop(env, worktree)
            finally:
                gitops.remove_worktree(self.workspace, worktree)

    def _run_turn_loop(self, env: Envelope, cwd: Path) -> str | None:
        corrections = self.drain_corrections()
        correction_block = ""
        if corrections:
            notes = "\n".join(
                f"- [{c.payload.get('rule_id')}] {c.payload.get('required_remedy')}: "
                f"{c.payload.get('note', '')} (re: event {c.payload.get('subject_event_id')})"
                for c in corrections
            )
            correction_block = (
                "\n\n== Sentinel corrections (already acked; apply them in this turn) ==\n"
                + notes
            )
        # static first, volatile last: the playbook is the same on every turn,
        # so anything that changes must sit AFTER it or the cached prefix dies
        base_prompt = self.playbook + "\n\n" + PROTOCOL_REMINDER.format(
            role=self.role,
            swarm=self.swarm,
            event_id=env.event_id,
            from_role=env.from_role,
            type=env.type,
            payload=json.dumps(env.payload, indent=2, sort_keys=True),
        ) + briefing.build(self.workspace, env.type, env.payload) + correction_block
        prompt = base_prompt

        def on_event(activity: str) -> None:
            print(f"[{time.strftime('%H:%M:%S')}]   {activity}", flush=True)
            self.heartbeat(status=f"{env.type}: {activity[:120]}")

        scope = env.behaviour_id or env.gate_id or env.type
        for _correction in range(MAX_CORRECTIONS + 1):
            session_ref = self._scoped_session(scope)
            started = time.monotonic()
            result = self.runner.run_turn(
                prompt=prompt,
                cwd=cwd,
                session_ref=session_ref,
                timeout_s=TURN_TIMEOUT_S,
                on_event=on_event,
            )
            duration_s = time.monotonic() - started
            self._save_session(result.session_ref)
            self._record_turn(scope, fresh=session_ref is None)
            self.report_usage(
                result, trigger_type=env.type, fresh_session=session_ref is None,
                session_turn=int(str(self._session_meta().get("turns", 1))),
                duration_s=duration_s, env=env,
            )
            if result.cost_usd:
                self._cost_total += float(result.cost_usd)
                print(f"[{time.strftime('%H:%M:%S')}] turn cost ${result.cost_usd:.2f} "
                      f"(worker total ${self._cost_total:.2f})", flush=True)

            reply_id = self._reply_on_stream(env.event_id)
            if reply_id is not None:
                return reply_id

            if result.budget_exhausted:
                # the ceiling is deliberate: correcting it would just spend the
                # same money again. Fail loudly and let a human decide.
                print(f"[{time.strftime('%H:%M:%S')}] !! {result.error}", flush=True)
                break

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
            self.role, "system", "worker.failed",
            {"role": self.role, "kind": "runner_failure",
             "detail": (result.error if result.budget_exhausted else
                        f"no reply published for {env.event_id} after {MAX_CORRECTIONS + 1} turns")},
        )
        dlq.route_to_dlq(
            self.client, self.publisher, self.swarm, self.role,
            "unparseable", env.to_fields(),
            f"model produced no on-stream reply after {MAX_CORRECTIONS + 1} attempts",
        )
        return None
