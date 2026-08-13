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
    RunInfo,
    RunPurpose,
    SwarmState,
    TERMINAL_STATES,
)
from relay.coordinator.policy import Policy

COORDINATOR = "coordinator"


def _new_run_id() -> str:
    return f"run-{ULID()}"


def _new_gate_id() -> str:
    return f"gate-{ULID()}"


@dataclass
class GitHooks:
    """Injected git behaviour so the dispatcher stays unit-testable.

    ensure_branch(iteration_id) -> head sha of the (possibly new) branch
    head_sha() -> current head of the iteration branch
    """

    ensure_branch: Callable[[str], str]
    head_sha: Callable[[], str]


class Dispatcher:
    def __init__(self, publisher: Publisher, policy: Policy, git: GitHooks) -> None:
        self._publisher = publisher
        self._policy = policy
        self._git = git
        self._branches_ensured: set[str] = set()
        self._last_progress: tuple[str, int] | None = None

    # ── entry point ──────────────────────────────────────────────────────────

    def react(self, state: SwarmState) -> int:
        """Publish whatever the protocol requires next. Returns publish count."""
        if not state.roadmap_committed:
            return 0
        if not self._roadmap_valid(state):
            self._publisher.send(
                COORDINATOR, "interpreter", "plan.roadmap_rejected",
                {"reasons": self._roadmap_errors(state)},
            )
            state.roadmap_committed = False
            return 1

        published = 0
        iteration = state.active_iteration()
        if iteration is None:
            return self._announce_completions(state)

        if iteration.id not in self._branches_ensured:
            self._git.ensure_branch(iteration.id)
            self._branches_ensured.add(iteration.id)

        published += self._advance_behaviours(state, iteration.id)
        published += self._announce_completions(state)
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

        # dispatch the next PLANNED behaviour when there is wip room; the INT
        # behaviour sits last in the order, so it is reached only when every
        # story behaviour is terminal.
        if len(in_flight) < self._policy.wip_limit:
            for b in behaviours:
                if b.state == BehaviourState.PLANNED:
                    published += self._dispatch_spec(b)
                    break
        return published

    def _dispatch_spec(self, b: Behaviour) -> int:
        self._publisher.send(
            COORDINATOR, "specifier", "work.spec_requested",
            {
                "behaviour_id": b.id,
                **({"story_id": b.story_id} if b.story_id else {}),
                "iteration_id": b.iteration_id,
                "ac_text": b.ac_text,
                "kind": b.kind,
                "base_sha": self._git.head_sha(),
            },
            behaviour_id=b.id, iteration_id=b.iteration_id, story_id=b.story_id,
        )
        b.state = BehaviourState.SPEC_DISPATCHED  # mirrored when own event replays
        return 1

    def _advance_one(self, state: SwarmState, b: Behaviour) -> int:
        if b.state == BehaviourState.SPEC_READY:
            return self._request_run(state, b, RunPurpose.RED_VERIFICATION)
        if b.state == BehaviourState.RED_FAILED:
            return self._dispatch_spec(b)
        if b.state == BehaviourState.RED_VERIFIED:
            self._publisher.send(
                COORDINATOR, "builder", "work.build_requested",
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
            return self._rework_or_escalate(b)
        if b.state in (BehaviourState.AT_GREEN, BehaviourState.GATES_PASSED):
            # Phase 1: no optional gates — straight to the specifier's judgement.
            return self._request_judgement(state, b)
        return 0

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

    def _rework_or_escalate(self, b: Behaviour) -> int:
        next_attempt = b.attempt + 1
        if next_attempt > self._policy.max_attempts:
            self._publisher.send(
                COORDINATOR, "interpreter", "plan.owner_decision_needed",
                {
                    "gate_id": _new_gate_id(),
                    "subject_id": b.id,
                    "reason": (
                        f"behaviour {b.id} blocked after {b.attempt} attempts: "
                        f"{b.last_fail_reason or 'repeated failure'}"
                    ),
                },
                behaviour_id=b.id, iteration_id=b.iteration_id,
            )
            b.state = BehaviourState.BLOCKED
            return 1
        self._publisher.send(
            COORDINATOR, "builder", "work.rework_requested",
            {
                "behaviour_id": b.id,
                "attempt": next_attempt,
                "findings": [{
                    "title": b.last_fail_reason or "behaviour not accepted",
                    "detail": b.last_fail_reason or "see previous verdicts on the ledger",
                    "severity": "major",
                    "source": "coordinator",
                }],
            },
            behaviour_id=b.id, iteration_id=b.iteration_id, story_id=b.story_id,
        )
        b.state = BehaviourState.BUILD_DISPATCHED
        b.attempt = next_attempt
        return 1

    def _request_judgement(self, state: SwarmState, b: Behaviour) -> int:
        green = [
            r for r in state.runs.values()
            if r.behaviour_id == b.id and r.purpose == RunPurpose.AT_GREEN and r.exit_code == 0
        ]
        if not green:
            return 0
        self._publisher.send(
            COORDINATOR, "specifier", "work.judgement_requested",
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

    # ── story / iteration completion + progress ──────────────────────────────

    def _announce_completions(self, state: SwarmState) -> int:
        published = 0
        for story in state.stories.values():
            if state.story_done(story.id) and not story.done_announced:
                behaviours = state.story_behaviours(story.id)
                self._publisher.send(
                    COORDINATOR, "interpreter", "plan.story_done",
                    {
                        "story_id": story.id,
                        "summary": f"{len(behaviours)} behaviours accepted for '{story.title}'.",
                    },
                    story_id=story.id, iteration_id=story.iteration_id,
                )
                story.done_announced = True
                published += 1
        for iteration in state.iterations.values():
            if (
                iteration.started
                and not iteration.aborted
                and state.iteration_done(iteration.id)
                and not iteration.ready_announced
            ):
                behaviours = state.iteration_behaviours(iteration.id)
                self._publisher.send(
                    COORDINATOR, "interpreter", "plan.iteration_ready",
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
        published += self._progress(state)
        return published

    def _progress(self, state: SwarmState) -> int:
        iteration = state.active_iteration()
        target = iteration or next(
            (it for it in state.iterations.values() if it.started), None
        )
        if target is None:
            return 0
        behaviours = state.iteration_behaviours(target.id)
        done = sum(1 for b in behaviours if b.state == BehaviourState.DONE)
        marker = (target.id, done)
        if marker == self._last_progress:
            return 0
        current = next((b.id for b in behaviours if b.state not in TERMINAL_STATES
                        and b.state != BehaviourState.PLANNED), "")
        blocked = [b.id for b in behaviours if b.state == BehaviourState.BLOCKED]
        self._publisher.send(
            COORDINATOR, "owner", "chat.progress",
            {
                "iteration_id": target.id,
                "behaviours_done": done,
                "behaviours_total": len(behaviours),
                **({"current": current} if current else {}),
                "blockers": blocked,
            },
            iteration_id=target.id,
        )
        self._last_progress = marker
        return 1


def _require(value: str | None, name: str) -> str:
    if value is None:
        raise RuntimeError(f"protocol hole: {name} missing when required")
    return value
