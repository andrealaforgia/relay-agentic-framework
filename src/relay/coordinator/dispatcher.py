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

from ulid import ULID

from relay.bus.publisher import Publisher
from relay.coordinator.model import (
    Behaviour,
    BehaviourState,
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


def _new_run_id() -> str:
    return f"run-{ULID()}"


def _new_gate_id() -> str:
    return f"gate-{ULID()}"


@dataclass
class GitHooks:
    """Injected git behaviour so the dispatcher stays unit-testable."""

    ensure_branch: Callable[[str], str]     # iteration_id -> branch head sha
    head_sha: Callable[[], str]
    has_history: Callable[[], bool]         # pre-existing codebase?
    create_pr: Callable[[str], str]         # iteration_id -> PR url


class Dispatcher:
    def __init__(self, publisher: Publisher, policy: Policy, git: GitHooks) -> None:
        self._publisher = publisher
        self._policy = policy
        self._git = git
        self._branches_ensured: set[str] = set()

    # ── entry point ──────────────────────────────────────────────────────────

    def react(self, state: SwarmState) -> int:
        published = self._maybe_request_recon(state)
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
            published += self._advance_behaviours(state, iteration.id)
        published += self._advance_stories(state)
        published += self._advance_iterations(state)
        published += self._progress(state)
        return published

    # ── legacy intake: reconnaissance before any roadmap ─────────────────────

    def _maybe_request_recon(self, state: SwarmState) -> int:
        if state.roadmap_committed or state.recon_requested or not self._git.has_history():
            return 0
        self._publisher.send(
            COORDINATOR, "analyst", "recon.requested",
            {"commit_sha": self._git.head_sha()},
        )
        state.recon_requested = True
        return 1

    # ── roadmap validation (lesson 4 lives in code) ─────────────────────────

    def _roadmap_errors(self, state: SwarmState) -> list[str]:
        errors: list[str] = []
        for story in state.stories.values():
            if not story.id.startswith(f"{story.iteration_id}."):
                errors.append(f"story {story.id} does not belong to iteration {story.iteration_id}")
        for b in state.behaviours.values():
            if b.story_id and not b.id.startswith(f"{b.story_id}."):
                errors.append(f"behaviour {b.id} does not belong to story {b.story_id}")
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
                    published += self._dispatch_spec(b)
                    break
        return published

    def _dispatch_spec(self, b: Behaviour) -> int:
        base = self._git.head_sha()
        self._publisher.send(
            COORDINATOR, "specifier", "spec.requested",
            {
                "behaviour_id": b.id,
                **({"story_id": b.story_id} if b.story_id else {}),
                "iteration_id": b.iteration_id,
                "ac_text": b.ac_text,
                "kind": b.kind,
                "base_sha": base,
            },
            behaviour_id=b.id, iteration_id=b.iteration_id, story_id=b.story_id,
        )
        b.state = BehaviourState.SPEC_DISPATCHED
        b.base_sha = base
        return 1

    def _advance_one(self, state: SwarmState, b: Behaviour) -> int:
        if b.state == BehaviourState.SPEC_READY:
            return self._request_run(state, b, RunPurpose.RED_VERIFICATION)
        if b.state == BehaviourState.RED_FAILED:
            return self._dispatch_spec(b)
        if b.state == BehaviourState.RED_VERIFIED:
            blocked = self._block_uncharacterized(state, b)
            if blocked:
                return blocked
            self._publisher.send(
                COORDINATOR, "builder", "build.requested",
                {
                    "behaviour_id": b.id,
                    "spec_commit_sha": _require(b.spec_commit, "spec_commit"),
                    "test_paths": b.test_paths,
                },
                behaviour_id=b.id, iteration_id=b.iteration_id, story_id=b.story_id,
            )
            b.state = BehaviourState.BUILD_DISPATCHED
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
        commit = b.spec_commit if purpose == RunPurpose.RED_VERIFICATION else b.built_commit
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
        b.state = (
            BehaviourState.RED_PENDING
            if purpose == RunPurpose.RED_VERIFICATION
            else BehaviourState.AT_RUN_PENDING
        )
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
        self._publisher.send(
            COORDINATOR, "builder", "rework.requested",
            {
                "behaviour_id": b.id,
                "attempt": next_attempt,
                "findings": [{
                    "title": reason,
                    "detail": f"{reason} — see the verdicts on the ledger",
                    "severity": "major",
                    "source": "coordinator",
                }],
            },
            behaviour_id=b.id, iteration_id=b.iteration_id, story_id=b.story_id,
        )
        b.state = BehaviourState.BUILD_DISPATCHED
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
            if specs:
                published += self._advance_story_gates(state, story, specs)
                if not story.gates_passed():
                    continue
            behaviours = state.story_behaviours(story.id)
            self._publisher.send(
                COORDINATOR, "interpreter", "story.completed",
                {
                    "story_id": story.id,
                    "summary": f"{len(behaviours)} behaviours accepted for '{story.title}'.",
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
            if specs:
                published += self._advance_iteration_gates(state, iteration, specs)
                if not iteration.gates_passed():
                    continue
            behaviours = state.iteration_behaviours(iteration.id)
            self._publisher.send(
                COORDINATOR, "interpreter", "iteration.finished",
                {
                    "iteration_id": iteration.id,
                    "summary": (
                        f"{len(behaviours)} behaviours done including the integration "
                        f"behaviour; increment: {iteration.increment}"
                    ),
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
                        f"review the findings and decide (fix, accept, or re-plan)"
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
