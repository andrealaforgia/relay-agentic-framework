"""The deterministic reaction engine.

After every consumed batch of events, `react(state)` computes what the
protocol requires next and publishes it. All decisions come from the
projection — never from memory of what this process did earlier — so a
restarted coordinator picks up mid-behaviour without double-dispatching:
its earlier dispatch events are in the ledger and therefore in the state.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ulid import ULID

from relay.bus.publisher import Publisher
from relay.coordinator.diagnosis import STATE_WAITS_ON, ts_epoch
from relay.coordinator.model import (
    Behaviour,
    BehaviourState,
    DecisionInfo,
    GateInfo,
    Iteration,
    RunInfo,
    RunPurpose,
    Story,
    SwarmState,
    TERMINAL_STATES,
)
from relay.coordinator.policy import GateSpec, Policy

COORDINATOR = "coordinator"


def _iso(now_s: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(now_s, UTC).isoformat(timespec="seconds")


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


def _new_run_id() -> str:
    return f"run-{ULID()}"


def _new_gate_id() -> str:
    return f"gate-{ULID()}"


def _findings_are_about_tests(findings: list[dict[str, object]] | None) -> bool:
    """True when every finding that names a file names a test file.

    Only the specifier may change tests. If the fix lives there, the builder
    cannot make it however many attempts it is given, so the loop burns its
    budget and the behaviour ends up blocked with nothing learned. Requiring
    ALL located findings to be test files keeps mixed findings — where
    production code is genuinely at fault too — with the builder.
    """
    located = [str(f.get("file", "")) for f in (findings or []) if f.get("file")]
    if not located:
        return False
    return all(
        path.startswith("tests/") or "/tests/" in path or Path(path).name.startswith("test_")
        for path in located
    )


@dataclass
class GitHooks:
    """Injected git behaviour so the dispatcher stays unit-testable."""

    ensure_branch: Callable[[str], str]     # iteration_id -> branch head sha
    head_sha: Callable[[], str]
    has_history: Callable[[], bool]         # pre-existing codebase?
    create_pr: Callable[[str], str]         # iteration_id -> PR url
    # curated knowledge (docs/relay/knowledge/, written by `relay learn`)
    # present? Recon is then redundant: the humans already offboarded more
    # than a scan would find.
    knowledge_exists: Callable[[], bool] = lambda: False


class Dispatcher:
    def __init__(self, publisher: Publisher, policy: Policy, git: GitHooks) -> None:
        self._publisher = publisher
        self._policy = policy
        self._git = git
        self._branches_ensured: set[str] = set()

    # ── entry point ──────────────────────────────────────────────────────────

    def react(self, state: SwarmState) -> int:
        published = self._maybe_request_recon(state)
        published += self._escalate_orphan_errors(state)
        published += self.reask_after_mismatch(state)
        if not state.roadmap_committed:
            return published
        if not self._roadmap_valid(state):
            self._publisher.send(
                COORDINATOR, "interpreter", "roadmap.rejected",
                {"reasons": self._roadmap_errors(state)},
            )
            state.roadmap_committed = False
            return published + 1

        iteration = state.active_iteration()
        if iteration is not None:
            if iteration.id not in self._branches_ensured:
                self._git.ensure_branch(iteration.id)
                self._branches_ensured.add(iteration.id)
            if self._plan_missing(iteration):
                published += self._nudge_for_plan(iteration)
            else:
                published += self._advance_behaviours(state, iteration.id)
        published += self._advance_stories(state)
        published += self._advance_iterations(state)
        published += self._progress(state)
        return published

    # ── supervision: waiting states police themselves ────────────────────────
    #
    # tick() runs on the coordinator's clock, not on events, so a swarm where
    # nothing arrives still supervises everything in flight. Every action it
    # takes is an ordinary ledger event, folded back into the projection —
    # a restart re-derives exactly which nudges already happened and never
    # spams. Three rules, applied uniformly:
    #   overdue and never re-dispatched  -> re-dispatch (fresh ids)
    #   overdue after a re-dispatch      -> escalate to the Owner (never drop)
    #   an open Owner decision           -> re-asked on its own interval, forever

    def tick(self, state: SwarmState, now_s: float) -> int:
        if not state.roadmap_committed:
            return 0
        published = self._nudge_open_decisions(state, now_s)
        published += self._supervise_behaviours(state, now_s)
        published += self._supervise_story_and_iteration_gates(state, now_s)
        return published

    def _overdue(self, since_iso: str, now_s: float, timeout_s: int) -> bool:
        return bool(since_iso) and (now_s - ts_epoch(since_iso)) > timeout_s

    def _nudge_open_decisions(self, state: SwarmState, now_s: float) -> int:
        published = 0
        for info in state.decisions.values():
            if info.closed:
                continue
            if self._overdue(info.last_ask, now_s, self._policy.decision_nudge_s):
                self._resend_decision(info, prefix=f"(reminder #{info.asks}) ")
                info.last_ask = _iso(now_s)   # mirrored when the event folds
                published += 1
        return published

    def _resend_decision(self, info: "DecisionInfo", prefix: str = "") -> None:
        self._publisher.send(
            COORDINATOR, "interpreter", "decision.requested",
            {"gate_id": info.gate_id, "subject_id": info.subject_id,
             "reason": (prefix + info.reason)[:2000]},
        )

    def reask_after_mismatch(self, state: SwarmState) -> int:
        """A decision.made matched nothing open: never let a human's answer
        evaporate — repeat every open ask immediately, with the exact syntax."""
        if not state.decision_mismatch:
            return 0
        state.decision_mismatch = False
        published = 0
        for info in state.decisions.values():
            if not info.closed:
                self._resend_decision(
                    info,
                    prefix=("your last decision matched nothing — reply exactly "
                            f"`retry {info.subject_id}` or `drop {info.subject_id}`. "),
                )
                published += 1
        return published

    def _supervise_behaviours(self, state: SwarmState, now_s: float) -> int:
        published = 0
        timeout = self._policy.dispatch_timeout_s
        for b in list(state.behaviours.values()):
            if b.state in TERMINAL_STATES:
                continue
            if b.state == BehaviourState.GATES_PENDING:
                published += self._redispatch_gates(
                    state, b.pending_gates, "behaviour", b.id, now_s,
                    commit=b.built_commit, base=b.base_sha,
                )
                continue
            waits_on = STATE_WAITS_ON.get(b.state, "coordinator")
            if waits_on == "coordinator":
                continue  # react() owns these; nothing was dispatched to wait for
            if not self._overdue(b.state_since, now_s, timeout):
                continue
            if b.same_state_dispatches >= 1:
                published += self._escalate_timeout(state, b.id, waits_on, b.state_since)
                continue
            published += self._redispatch_behaviour(state, b)
        return published

    def _redispatch_behaviour(self, state: SwarmState, b: Behaviour) -> int:
        if b.state == BehaviourState.SPEC_DISPATCHED:
            return self._dispatch_spec(b)
        if b.state == BehaviourState.RED_PENDING:
            return self._request_run(state, b, RunPurpose.RED_VERIFICATION)
        if b.state == BehaviourState.SATISFIED_PENDING:
            return self._request_run(state, b, RunPurpose.SATISFIED_CHECK)
        if b.state == BehaviourState.AT_RUN_PENDING:
            return self._request_run(state, b, RunPurpose.AT_GREEN)
        if b.state == BehaviourState.BUILD_DISPATCHED:
            self._publisher.send(
                COORDINATOR, "builder", "build.requested",
                {
                    "behaviour_id": b.id,
                    "spec_commit_sha": _require(b.spec_commit, "spec_commit"),
                    "test_paths": b.test_paths,
                },
                behaviour_id=b.id, iteration_id=b.iteration_id, story_id=b.story_id,
            )
            return 1
        if b.state == BehaviourState.ACCEPTANCE_PENDING:
            return self._request_judgement(state, b)
        return 0

    def _redispatch_gates(
        self,
        state: SwarmState,
        pending: dict[str, GateInfo],
        subject_kind: str,
        subject_id: str,
        now_s: float,
        commit: str | None,
        base: str | None,
        run_id: str | None = None,
    ) -> int:
        published = 0
        specs = {s.gate: s for s in (*self._policy.per_behaviour,
                                     *self._policy.per_story, *self._policy.per_iteration)}
        for g in list(pending.values()):
            if g.verdict is not None:
                continue
            spec = specs.get(g.gate)
            timeout = spec.timeout_s if spec else self._policy.dispatch_timeout_s
            if not self._overdue(g.since, now_s, timeout):
                continue
            if g.attempt >= 1:
                published += self._escalate_timeout(
                    state, subject_id, spec.role if spec else g.gate, g.since)
                continue
            gate_id = _new_gate_id()
            self._publisher.send(
                COORDINATOR, spec.role if spec else "qa", "gate.requested",
                {
                    "gate_id": gate_id, "gate": g.gate,
                    "subject_kind": subject_kind, "subject_id": subject_id,
                    "commit_sha": _require(commit, "gate commit"),
                    "base_sha": _require(base, "gate base"),
                    **({"run_id": run_id} if run_id else {}),
                },
                gate_id=gate_id, commit_sha=commit,
            )
            published += 1
        return published

    def _supervise_story_and_iteration_gates(self, state: SwarmState, now_s: float) -> int:
        published = 0
        for story in state.stories.values():
            if story.escalated:
                continue
            if story.mutation_run_id:
                run = state.runs.get(story.mutation_run_id)
                if run is not None and run.exit_code is None and self._overdue(
                    run.since, now_s, self._policy.dispatch_timeout_s
                ):
                    behaviours = state.story_behaviours(story.id)
                    run_id = _new_run_id()
                    self._publisher.send(
                        COORDINATOR, "toolgate", "run.requested",
                        {"run_id": run_id, "kind": "mutation",
                         "commit_sha": self._last_built_commit(behaviours)},
                        story_id=story.id, iteration_id=story.iteration_id,
                    )
                    published += 1
            if story.pending_gates:
                behaviours = state.story_behaviours(story.id)
                published += self._redispatch_gates(
                    state, story.pending_gates, "story", story.id, now_s,
                    commit=self._last_built_commit(behaviours),
                    base=self._first_base_sha(behaviours),
                    run_id=story.mutation_run_id,
                )
        for iteration in state.iterations.values():
            if iteration.escalated or not iteration.pending_gates:
                continue
            behaviours = state.iteration_behaviours(iteration.id)
            published += self._redispatch_gates(
                state, iteration.pending_gates, "iteration", iteration.id, now_s,
                commit=self._last_built_commit(behaviours),
                base=self._first_base_sha(behaviours),
            )
        return published

    def _escalate_timeout(
        self, state: SwarmState, subject_id: str, waits_on: str, since: str
    ) -> int:
        for info in state.decisions.values():
            if not info.closed and info.subject_id == subject_id:
                return 0                      # already on the Owner's desk
        gate_id = _new_gate_id()
        # mirror before the fold, so one tick never asks twice for one subject
        state.decisions[gate_id] = DecisionInfo(
            gate_id=gate_id, subject_id=subject_id, reason="", since="", last_ask="",
            asks=0,
        )
        self._publisher.send(
            COORDINATOR, "interpreter", "decision.requested",
            {
                "gate_id": gate_id,
                "subject_id": subject_id,
                "reason": (
                    f"{subject_id} has waited on {waits_on} since {since} and one "
                    f"re-dispatch changed nothing — the worker may be wedged or its "
                    f"turns failing. Reply exactly `retry {subject_id}` (fresh attempt "
                    f"budget) or `drop {subject_id}`; check `relay tail {waits_on}` "
                    f"for the underlying failure."
                ),
            },
        )
        return 1

    # ── plan mode: no behaviour without an Owner-approved change plan ────────

    def _plan_missing(self, iteration: Iteration) -> bool:
        return self._policy.plan_required and iteration.plan_path is None

    def _nudge_for_plan(self, iteration: Iteration) -> int:
        """Tell the Owner (via the Interpreter) exactly once per iteration;
        the projection folds the nudge, so a restart never re-nags."""
        if iteration.plan_nudged:
            return 0
        self._publisher.send(
            COORDINATOR, "interpreter", "stall.detected",
            {
                "subject_id": iteration.id,
                "waiting_on": "planner",
                "since_ts": _now_iso(),
            },
            iteration_id=iteration.id,
        )
        iteration.plan_nudged = True
        return 1

    # ── legacy intake: reconnaissance before any roadmap ─────────────────────

    def _maybe_request_recon(self, state: SwarmState) -> int:
        if state.roadmap_committed or state.recon_requested or not self._git.has_history():
            return 0
        if self._git.knowledge_exists():
            return 0
        self._publisher.send(
            COORDINATOR, "analyst", "recon.requested",
            {"commit_sha": self._git.head_sha()},
        )
        state.recon_requested = True
        return 1

    def _escalate_orphan_errors(self, state: SwarmState) -> int:
        """error.raised without a behaviour still reaches the owner, exactly once
        (the escalation carries source_event_id; replay clears it)."""
        published = 0
        for event_id, detail in list(state.unescalated_errors.items()):
            self._publisher.send(
                COORDINATOR, "interpreter", "decision.requested",
                {"gate_id": _new_gate_id(), "subject_id": "swarm",
                 "reason": f"an assistant reported an error: {detail}",
                 "source_event_id": event_id},
            )
            del state.unescalated_errors[event_id]
            published += 1
        return published

    # ── roadmap validation (lesson 4 lives in code) ─────────────────────────

    def _roadmap_errors(self, state: SwarmState) -> list[str]:
        errors: list[str] = []
        for story in state.stories.values():
            if not story.id.startswith(f"{story.iteration_id}."):
                errors.append(f"story {story.id} does not belong to iteration {story.iteration_id}")
        for b in state.behaviours.values():
            if b.story_id and not b.id.startswith(f"{b.story_id}."):
                errors.append(f"behaviour {b.id} does not belong to story {b.story_id}")
        if state.roadmap_wrote_integration:
            errors.append(
                f"the roadmap writes integration behaviours "
                f"({', '.join(sorted(set(state.roadmap_wrote_integration)))}): integration "
                f"behaviours are created by the coordinator, one per story and one per "
                f"iteration — do not write them into the roadmap"
            )
        for iteration in state.iterations.values():
            if not iteration.increment.strip():
                errors.append(f"iteration {iteration.id} has no demonstrable increment")
        return errors

    def _roadmap_valid(self, state: SwarmState) -> bool:
        return not self._roadmap_errors(state)

    # ── behaviour advancement ────────────────────────────────────────────────

    def _advance_behaviours(self, state: SwarmState, iteration_id: str) -> int:
        behaviours = state.iteration_behaviours(iteration_id)
        in_flight = [
            b for b in behaviours
            if b.state not in TERMINAL_STATES and b.state != BehaviourState.PLANNED
        ]

        published = 0
        for b in in_flight:
            published += self._advance_one(state, b)

        if len(in_flight) < self._policy.wip_limit:
            for b in behaviours:
                if b.state == BehaviourState.PLANNED:
                    published += self._dispatch_spec(b, state)
                    break
        return published

    def _spec_batch(self, state: SwarmState, b: Behaviour) -> list[Behaviour]:
        """The behaviours this one request covers.

        At story granularity the specifier is handed the whole story: it
        explores the codebase once and writes every failing test in one turn,
        still publishing one spec.written per behaviour. At behaviour
        granularity it is handed one.
        """
        if self._policy.spec_granularity != "story" or not b.story_id:
            return [b]
        return [
            other for other in state.story_behaviours(b.story_id)
            if other.state == BehaviourState.PLANNED
        ] or [b]

    def _dispatch_spec(self, b: Behaviour, state: SwarmState | None = None) -> int:
        base = self._git.head_sha()
        batch = self._spec_batch(state, b) if state is not None else [b]
        payload: dict[str, object] = {
            "behaviour_id": b.id,
            **({"story_id": b.story_id} if b.story_id else {}),
            "iteration_id": b.iteration_id,
            "ac_text": b.ac_text,
            "kind": b.kind,
            "base_sha": base,
        }
        if len(batch) > 1:
            payload["criteria"] = [
                {"behaviour_id": other.id, "ac_text": other.ac_text, "kind": other.kind}
                for other in batch
            ]
        self._publisher.send(
            COORDINATOR, "specifier", "spec.requested", payload,
            behaviour_id=b.id, iteration_id=b.iteration_id, story_id=b.story_id,
        )
        for other in batch:
            other.state = BehaviourState.SPEC_DISPATCHED
            other.base_sha = base
        return 1

    def _advance_one(self, state: SwarmState, b: Behaviour) -> int:
        if b.spec_conflict is not None:
            # an existing acceptance test contradicts this behaviour: only the
            # specifier may retire or amend it, so this is rework, not a
            # question for the Owner
            detail, b.spec_conflict = b.spec_conflict, None
            b.last_findings = [{
                "severity": "major",
                "title": "an existing acceptance test contradicts this behaviour",
                "detail": detail,
                "source": "builder",
            }]
            b.last_fail_gate = "test_design"
            return self._rework_or_escalate(b, "existing acceptance tests contradict this behaviour")
        if b.error_reported is not None:
            # an assistant said it is stuck: that must never vanish (fail loud)
            reason = b.error_reported
            b.error_reported = None
            self._publisher.send(
                COORDINATOR, "interpreter", "decision.requested",
                {"gate_id": _new_gate_id(), "subject_id": b.id,
                 "reason": f"assistant reported an error on {b.id}: {reason}"},
                behaviour_id=b.id, iteration_id=b.iteration_id,
            )
            b.state = BehaviourState.BLOCKED
            return 1
        if b.state == BehaviourState.SPEC_READY:
            return self._request_run(state, b, RunPurpose.RED_VERIFICATION)
        if b.state == BehaviourState.SATISFIED_CLAIMED:
            return self._request_run(state, b, RunPurpose.SATISFIED_CHECK)
        if b.state == BehaviourState.RED_FAILED:
            if b.spec_attempts >= self._policy.max_attempts:
                self._publisher.send(
                    COORDINATOR, "interpreter", "decision.requested",
                    {"gate_id": _new_gate_id(), "subject_id": b.id,
                     "reason": (f"behaviour {b.id} failed red-verification "
                                f"{b.spec_attempts} times: "
                                f"{b.last_fail_reason or 'spec loop'} — re-scope, mark as "
                                f"already covered, or drop it")},
                    behaviour_id=b.id, iteration_id=b.iteration_id,
                )
                b.state = BehaviourState.BLOCKED
                return 1
            return self._dispatch_spec(b)
        if b.state == BehaviourState.RED_VERIFIED:
            blocked = self._block_uncharacterized(state, b)
            if blocked:
                return blocked
            batch = self._build_batch(state, b)
            if not batch:
                return 0            # story granularity: wait for the rest to go red
            payload: dict[str, object] = {
                "behaviour_id": b.id,
                "spec_commit_sha": _require(b.spec_commit, "spec_commit"),
                "test_paths": b.test_paths,
            }
            if len(batch) > 1:
                payload["behaviours"] = [
                    {"behaviour_id": other.id, "test_paths": other.test_paths}
                    for other in batch
                ]
            self._publisher.send(
                COORDINATOR, "builder", "build.requested", payload,
                behaviour_id=b.id, iteration_id=b.iteration_id, story_id=b.story_id,
            )
            for other in batch:
                other.state = BehaviourState.BUILD_DISPATCHED
            return 1
        if b.state == BehaviourState.BUILT:
            return self._request_run(state, b, RunPurpose.AT_GREEN)
        if b.state == BehaviourState.AT_RED:
            return self._rework_or_escalate(b, b.last_fail_reason or "behaviour not accepted")
        if b.state == BehaviourState.AT_GREEN:
            if self._policy.per_behaviour and not b.pending_gates:
                return self._request_behaviour_gates(b)
            if not self._policy.per_behaviour:
                return self._request_judgement(state, b)
            return 0
        if b.state == BehaviourState.GATES_PASSED:
            return self._request_judgement(state, b)
        return 0

    def _build_batch(self, state: SwarmState, b: Behaviour) -> list[Behaviour]:
        """The behaviours this build covers, or nothing if it is not time yet.

        At story granularity the builder gets every red test of the story at
        once, so it acquires the codebase once and then works behaviour by
        behaviour inside a warm session. That means waiting until they have ALL
        gone red — half a story is not a story.
        """
        if self._policy.build_granularity != "story" or not b.story_id:
            return [b]
        siblings = [
            other for other in state.story_behaviours(b.story_id)
            if other.state not in TERMINAL_STATES
        ]
        if any(other.state != BehaviourState.RED_VERIFIED for other in siblings):
            return []
        return siblings

    def _block_uncharacterized(self, state: SwarmState, b: Behaviour) -> int:
        """Never touch legacy risk areas without characterization tests —
        a dispatcher rule, not a playbook sentence (DECISIONS D12)."""
        if b.kind != "ac" or not state.risk_areas or b.story_id is None:
            return 0
        touched_risks = sorted(set(b.touches) & set(state.risk_areas))
        if not touched_risks or state.story_char_done(b.story_id):
            return 0
        self._publisher.send(
            COORDINATOR, "interpreter", "decision.requested",
            {
                "gate_id": _new_gate_id(),
                "subject_id": b.id,
                "reason": (
                    f"behaviour {b.id} would change legacy risk areas "
                    f"({', '.join(touched_risks)}) with no characterization tests in its "
                    f"story — add a characterization behaviour (e.g. {b.story_id}.CHAR1) "
                    f"or explicitly accept the risk"
                ),
            },
            behaviour_id=b.id, iteration_id=b.iteration_id,
        )
        b.state = BehaviourState.BLOCKED
        return 1

    def _request_behaviour_gates(self, b: Behaviour) -> int:
        published = 0
        for spec in self._policy.per_behaviour:
            gate_id = _new_gate_id()
            self._publisher.send(
                COORDINATOR, spec.role, "gate.requested",
                {
                    "gate_id": gate_id,
                    "gate": spec.gate,
                    "subject_kind": "behaviour",
                    "subject_id": b.id,
                    "commit_sha": _require(b.built_commit, "built_commit"),
                    "base_sha": _require(b.base_sha, "base_sha"),
                },
                behaviour_id=b.id, iteration_id=b.iteration_id, story_id=b.story_id,
                gate_id=gate_id, commit_sha=b.built_commit,
            )
            b.pending_gates[gate_id] = GateInfo(gate_id=gate_id, gate=spec.gate, subject_id=b.id)
            published += 1
        b.state = BehaviourState.GATES_PENDING
        return published

    def _request_run(self, state: SwarmState, b: Behaviour, purpose: RunPurpose) -> int:
        # Red-verification asks "does this test fail where it was introduced?",
        # so it pins to the spec commit. A satisfied claim asks "does the
        # criterion hold NOW?" — pinning that to the spec commit checks a tree
        # that predates any implementation landing later, so a behaviour whose
        # code arrived with a neighbouring one can never prove itself and
        # blocks after three identical attempts.
        if purpose == RunPurpose.RED_VERIFICATION:
            commit = b.spec_commit
        elif purpose == RunPurpose.SATISFIED_CHECK:
            commit = self._git.head_sha() or b.spec_commit
        else:
            commit = b.built_commit
        run_id = _new_run_id()
        self._publisher.send(
            COORDINATOR, "toolgate", "run.requested",
            {
                "run_id": run_id,
                "kind": "acceptance_test",
                "commit_sha": _require(commit, "commit"),
                "test_paths": b.test_paths,
                "behaviour_id": b.id,
            },
            behaviour_id=b.id, iteration_id=b.iteration_id, story_id=b.story_id,
            commit_sha=commit,
        )
        state.runs[run_id] = RunInfo(run_id=run_id, purpose=purpose, behaviour_id=b.id)
        b.state = {
            RunPurpose.RED_VERIFICATION: BehaviourState.RED_PENDING,
            RunPurpose.SATISFIED_CHECK: BehaviourState.SATISFIED_PENDING,
        }.get(purpose, BehaviourState.AT_RUN_PENDING)
        return 1

    def _rework_or_escalate(self, b: Behaviour, reason: str) -> int:
        next_attempt = b.attempt + 1
        if next_attempt > self._policy.max_attempts:
            self._publisher.send(
                COORDINATOR, "interpreter", "decision.requested",
                {
                    "gate_id": _new_gate_id(),
                    "subject_id": b.id,
                    "reason": f"behaviour {b.id} blocked after {b.attempt} attempts: {reason}",
                },
                behaviour_id=b.id, iteration_id=b.iteration_id,
            )
            b.state = BehaviourState.BLOCKED
            return 1
        # Rework goes to whoever can act on it. test_design and mutation
        # findings are about the TESTS, which only the specifier may touch —
        # sending them to the builder asks for a change its own playbook
        # forbids, so the loop cannot converge and burns three attempts.
        # A code_review finding can also be about the tests: removing something
        # the existing acceptance tests observe is a code change whose fix lives
        # in the test files. Routing that to the builder is the same dead end,
        # so the findings themselves decide when the gate name does not.
        culprit = (
            "specifier"
            if b.last_fail_gate in ("test_design", "mutation")
            or _findings_are_about_tests(b.last_findings)
            else "builder"
        )
        findings = b.last_findings or [{
            "title": reason,
            "detail": f"{reason} — see the verdicts on the ledger",
            "severity": "major",
            "source": "coordinator",
        }]
        self._publisher.send(
            COORDINATOR, culprit, "rework.requested",
            {
                "behaviour_id": b.id,
                "attempt": next_attempt,
                "findings": findings,
            },
            behaviour_id=b.id, iteration_id=b.iteration_id, story_id=b.story_id,
        )
        b.state = (BehaviourState.SPEC_DISPATCHED if culprit == "specifier"
                   else BehaviourState.BUILD_DISPATCHED)
        b.attempt = next_attempt
        b.pending_gates.clear()
        return 1

    def _request_judgement(self, state: SwarmState, b: Behaviour) -> int:
        green = [
            r for r in state.runs.values()
            if r.behaviour_id == b.id and r.purpose == RunPurpose.AT_GREEN and r.exit_code == 0
        ]
        if not green:
            return 0
        self._publisher.send(
            COORDINATOR, "specifier", "judgement.requested",
            {
                "behaviour_id": b.id,
                "commit_sha": _require(b.built_commit, "built_commit"),
                "run_id": green[-1].run_id,
            },
            behaviour_id=b.id, iteration_id=b.iteration_id, story_id=b.story_id,
            commit_sha=b.built_commit,
        )
        b.state = BehaviourState.ACCEPTANCE_PENDING
        return 1

    # ── story completion: behaviours done -> mutation gate -> announce ──────

    def _advance_stories(self, state: SwarmState) -> int:
        published = 0
        for story in state.stories.values():
            if story.done_announced or story.escalated:
                continue
            if not state.story_behaviours_done(story.id):
                continue
            specs = self._policy.per_story
            if specs and not story.gates_waived:
                published += self._advance_story_gates(state, story, specs)
                if not story.gates_passed():
                    continue
            behaviours = state.story_behaviours(story.id)
            how_to_try = state.story_how_to_try(story.id)
            self._publisher.send(
                COORDINATOR, "interpreter", "story.completed",
                {
                    "story_id": story.id,
                    "summary": f"{len(behaviours)} behaviours accepted for '{story.title}'.",
                    **({"how_to_try": how_to_try} if how_to_try else {}),
                },
                story_id=story.id, iteration_id=story.iteration_id,
            )
            story.done_announced = True
            published += 1
        return published

    def _advance_story_gates(
        self, state: SwarmState, story: Story, specs: tuple[GateSpec, ...]
    ) -> int:
        last_commit = self._last_built_commit(state.story_behaviours(story.id))
        base = self._first_base_sha(state.story_behaviours(story.id))
        if story.mutation_run_id is None:
            run_id = _new_run_id()
            self._publisher.send(
                COORDINATOR, "toolgate", "run.requested",
                {"run_id": run_id, "kind": "mutation", "commit_sha": last_commit},
                story_id=story.id, iteration_id=story.iteration_id, commit_sha=last_commit,
            )
            story.mutation_run_id = run_id
            state.runs[run_id] = RunInfo(run_id=run_id, purpose=RunPurpose.MUTATION,
                                         story_id=story.id)
            return 1
        run = state.runs.get(story.mutation_run_id)
        if run is None or run.exit_code is None:
            return 0  # waiting on the toolgate
        if not story.pending_gates:
            published = 0
            for spec in specs:
                gate_id = _new_gate_id()
                self._publisher.send(
                    COORDINATOR, spec.role, "gate.requested",
                    {
                        "gate_id": gate_id,
                        "gate": spec.gate,
                        "subject_kind": "story",
                        "subject_id": story.id,
                        "commit_sha": last_commit,
                        "base_sha": base,
                        "run_id": story.mutation_run_id,
                    },
                    story_id=story.id, iteration_id=story.iteration_id,
                    gate_id=gate_id, commit_sha=last_commit,
                )
                story.pending_gates[gate_id] = GateInfo(
                    gate_id=gate_id, gate=spec.gate, subject_id=story.id
                )
                published += 1
            return published
        if story.gates_failed():
            behaviours = [b for b in state.story_behaviours(story.id) if b.kind == "ac"]
            target = behaviours[-1] if behaviours else state.story_behaviours(story.id)[-1]
            failed = [g.gate for g in story.pending_gates.values() if g.verdict == "fail"]
            story.reset_gates()
            return self._rework_or_escalate(
                target, f"story gate failed: {', '.join(failed)} (see gate findings)"
            )
        return 0

    # ── iteration completion: security gate -> ready -> PR ──────────────────

    def _advance_iterations(self, state: SwarmState) -> int:
        published = 0
        for iteration in state.iterations.values():
            if not iteration.started or iteration.aborted:
                continue
            if iteration.pr_approved and not iteration.pr_opened:
                url = self._git.create_pr(iteration.id)
                self._publisher.send(
                    COORDINATOR, "interpreter", "pr.opened",
                    {"iteration_id": iteration.id, "pr_url": url},
                    iteration_id=iteration.id,
                )
                iteration.pr_opened = True
                published += 1
            if iteration.ready_announced or iteration.escalated:
                continue
            if not state.behaviours_done(iteration.id):
                continue
            stories_done = all(
                state.stories[sid].done_announced for sid in iteration.story_ids
            )
            if not stories_done:
                continue
            specs = self._policy.per_iteration
            if specs and not iteration.gates_waived:
                published += self._advance_iteration_gates(state, iteration, specs)
                if not iteration.gates_passed():
                    continue
            behaviours = state.iteration_behaviours(iteration.id)
            how_to_try = state.how_to_try(iteration.id)
            self._publisher.send(
                COORDINATOR, "interpreter", "iteration.finished",
                {
                    "iteration_id": iteration.id,
                    "summary": (
                        f"{len(behaviours)} behaviours done including the integration "
                        f"behaviour; increment: {iteration.increment}"
                    ),
                    **({"how_to_try": how_to_try} if how_to_try else {}),
                },
                iteration_id=iteration.id,
            )
            iteration.ready_announced = True
            published += 1
        return published

    def _advance_iteration_gates(
        self, state: SwarmState, iteration: Iteration, specs: tuple[GateSpec, ...]
    ) -> int:
        behaviours = state.iteration_behaviours(iteration.id)
        last_commit = self._last_built_commit(behaviours)
        base = self._first_base_sha(behaviours)
        if not iteration.pending_gates:
            published = 0
            for spec in specs:
                gate_id = _new_gate_id()
                self._publisher.send(
                    COORDINATOR, spec.role, "gate.requested",
                    {
                        "gate_id": gate_id,
                        "gate": spec.gate,
                        "subject_kind": "iteration",
                        "subject_id": iteration.id,
                        "commit_sha": last_commit,
                        "base_sha": base,
                    },
                    iteration_id=iteration.id, gate_id=gate_id, commit_sha=last_commit,
                )
                iteration.pending_gates[gate_id] = GateInfo(
                    gate_id=gate_id, gate=spec.gate, subject_id=iteration.id
                )
                published += 1
            return published
        if iteration.gates_failed():
            failed = [g.gate for g in iteration.pending_gates.values() if g.verdict == "fail"]
            self._publisher.send(
                COORDINATOR, "interpreter", "decision.requested",
                {
                    "gate_id": _new_gate_id(),
                    "subject_id": iteration.id,
                    "reason": (
                        f"iteration {iteration.id} gate failed: {', '.join(failed)} — "
                        f"review the findings, then reply exactly `retry {iteration.id}` "
                        f"(run the gate again, e.g. after fixes land) or "
                        f"`drop {iteration.id}` (waive this gate and proceed)"
                    ),
                },
                iteration_id=iteration.id,
            )
            iteration.escalated = True
            return 1
        return 0

    # ── shared helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _last_built_commit(behaviours: list[Behaviour]) -> str:
        commits = [b.built_commit for b in behaviours if b.built_commit]
        return _require(commits[-1] if commits else None, "built commit for gate")

    @staticmethod
    def _first_base_sha(behaviours: list[Behaviour]) -> str:
        shas = [b.base_sha for b in behaviours if b.base_sha]
        return _require(shas[0] if shas else None, "base sha for gate")

    def _progress(self, state: SwarmState) -> int:
        iteration = state.active_iteration() or next(
            (it for it in state.iterations.values() if it.started), None
        )
        if iteration is None:
            return 0
        behaviours = state.iteration_behaviours(iteration.id)
        done = sum(1 for b in behaviours if b.state == BehaviourState.DONE)
        marker = (iteration.id, done)
        if marker == state.last_progress:
            return 0
        current = next((b.id for b in behaviours if b.state not in TERMINAL_STATES
                        and b.state != BehaviourState.PLANNED), "")
        blocked = [b.id for b in behaviours if b.state == BehaviourState.BLOCKED]
        self._publisher.send(
            COORDINATOR, "owner", "progress.reported",
            {
                "iteration_id": iteration.id,
                "behaviours_done": done,
                "behaviours_total": len(behaviours),
                **({"current": current} if current else {}),
                "blockers": blocked,
            },
            iteration_id=iteration.id,
        )
        state.last_progress = marker
        return 1


def _require(value: str | None, name: str) -> str:
    if value is None:
        raise RuntimeError(f"protocol hole: {name} missing when required")
    return value
