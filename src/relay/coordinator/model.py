"""The work-state model the coordinator projects from the ledger.

Everything here is derived state: it can always be rebuilt by folding the
ledger from seq 1 (docs/DECISIONS.md D3). Nothing in this module talks to
Redis, git, or a model — it is pure data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class BehaviourState(StrEnum):
    PLANNED = "planned"
    SPEC_DISPATCHED = "spec_dispatched"
    SPEC_READY = "spec_ready"
    RED_PENDING = "red_pending"          # red-verification run dispatched to toolgate
    RED_FAILED = "red_failed"            # verification outcome wrong — back to specifier
    RED_VERIFIED = "red_verified"
    SATISFIED_CLAIMED = "satisfied_claimed"   # specifier: criterion already holds
    SATISFIED_PENDING = "satisfied_pending"   # toolgate verifying the guard test is green
    BUILD_DISPATCHED = "build_dispatched"
    BUILT = "built"
    AT_RUN_PENDING = "at_run_pending"    # post-build AT run dispatched to toolgate
    AT_RED = "at_red"                    # AT failing / a gate failed — rework
    AT_GREEN = "at_green"
    GATES_PENDING = "gates_pending"
    GATES_PASSED = "gates_passed"
    ACCEPTANCE_PENDING = "acceptance_pending"
    DONE = "done"
    BLOCKED = "blocked"


TERMINAL_STATES = frozenset({BehaviourState.DONE, BehaviourState.BLOCKED})


class RunPurpose(StrEnum):
    RED_VERIFICATION = "red_verification"
    AT_GREEN = "at_green"
    MUTATION = "mutation"
    SATISFIED_CHECK = "satisfied_check"


@dataclass
class RunInfo:
    run_id: str
    purpose: RunPurpose
    behaviour_id: str | None = None
    story_id: str | None = None
    exit_code: int | None = None


@dataclass
class GateInfo:
    gate_id: str
    gate: str            # code_review | test_design | mutation | security
    subject_id: str
    verdict: str | None = None  # pass | fail


@dataclass
class Behaviour:
    id: str
    iteration_id: str
    story_id: str | None            # None for INT behaviours
    kind: str                        # ac | integration | characterization
    ac_text: str
    title: str = ""                  # the outcome in a few words (board summary)
    state: BehaviourState = BehaviourState.PLANNED
    attempt: int = 1
    spec_attempts: int = 0               # spec.requested dispatches (respec-loop cap)
    error_reported: str | None = None    # unresolved error.raised detail
    spec_conflict: str | None = None     # an existing test contradicts this
    test_paths: list[str] = field(default_factory=list)
    touches: list[str] = field(default_factory=list)
    base_sha: str | None = None
    spec_commit: str | None = None
    built_commit: str | None = None
    how_to_run: str = ""             # from the builder: exact commands to try it
    pending_gates: dict[str, GateInfo] = field(default_factory=dict)
    last_fail_reason: str | None = None
    # the failing gate and its actual findings: rework is unactionable without
    # them, and which gate failed decides WHO can act on it
    last_fail_gate: str | None = None
    last_findings: list[dict[str, object]] = field(default_factory=list)


@dataclass
class Story:
    id: str
    iteration_id: str
    title: str
    behaviour_ids: list[str] = field(default_factory=list)
    int_behaviour_id: str = ""          # the story's own end-to-end behaviour
    done_announced: bool = False        # story.completed seen on the ledger
    mutation_run_id: str | None = None  # in-flight or judged mutation run
    pending_gates: dict[str, GateInfo] = field(default_factory=dict)
    escalated: bool = False

    def gates_passed(self) -> bool:
        return bool(self.pending_gates) and all(
            g.verdict == "pass" for g in self.pending_gates.values()
        )

    def gates_failed(self) -> bool:
        return any(g.verdict == "fail" for g in self.pending_gates.values())

    def reset_gates(self) -> None:
        self.mutation_run_id = None
        self.pending_gates.clear()


@dataclass
class Iteration:
    id: str
    goal: str
    increment: str
    story_ids: list[str] = field(default_factory=list)
    int_behaviour_id: str = ""
    started: bool = False
    ready_announced: bool = False       # iteration.finished seen on the ledger
    aborted: bool = False
    pending_gates: dict[str, GateInfo] = field(default_factory=dict)
    escalated: bool = False
    pr_approved: bool = False           # pr.approved seen
    pr_opened: bool = False             # pr.opened seen
    plan_path: str | None = None        # plan.committed seen (plan mode)
    plan_nudged: bool = False           # stall.detected(waiting_on=planner) seen

    def gates_passed(self) -> bool:
        return bool(self.pending_gates) and all(
            g.verdict == "pass" for g in self.pending_gates.values()
        )

    def gates_failed(self) -> bool:
        return any(g.verdict == "fail" for g in self.pending_gates.values())


@dataclass
class SwarmState:
    """The projection. `last_seq` makes snapshots verifiable."""

    last_seq: int = 0
    last_event_id: str | None = None
    roadmap_committed: bool = False
    intake_mode: str = "greenfield"
    recon_requested: bool = False
    recon_done: bool = False
    risk_areas: list[str] = field(default_factory=list)
    iterations: dict[str, Iteration] = field(default_factory=dict)
    stories: dict[str, Story] = field(default_factory=dict)
    behaviours: dict[str, Behaviour] = field(default_factory=dict)
    runs: dict[str, RunInfo] = field(default_factory=dict)
    # ordering as authored in the roadmap; INT behaviours appended last per iteration
    behaviour_order: list[str] = field(default_factory=list)
    # integration behaviours a model wrote into the roadmap: skipped on the
    # way in (code makes its own) and reported as a validation error
    roadmap_wrote_integration: list[str] = field(default_factory=list)
    # last progress.reported announced, as (iteration_id, behaviours_done) — derived
    # from the ledger so a restarted coordinator never re-announces
    last_progress: tuple[str, int] | None = None
    # error.raised events without a behaviour, not yet escalated (event_id -> detail);
    # the coordinator's decision.requested carries source_event_id, which clears them
    unescalated_errors: dict[str, str] = field(default_factory=dict)

    def iteration_behaviours(self, iteration_id: str) -> list[Behaviour]:
        return [
            self.behaviours[bid]
            for bid in self.behaviour_order
            if self.behaviours[bid].iteration_id == iteration_id
        ]

    def story_behaviours(self, story_id: str) -> list[Behaviour]:
        return [self.behaviours[bid] for bid in self.stories[story_id].behaviour_ids]

    def active_iteration(self) -> Iteration | None:
        for iteration in self.iterations.values():
            if iteration.started and not iteration.aborted and not iteration.ready_announced:
                return iteration
        return None

    def behaviours_done(self, iteration_id: str) -> bool:
        behaviours = self.iteration_behaviours(iteration_id)
        return bool(behaviours) and all(b.state == BehaviourState.DONE for b in behaviours)

    def story_behaviours_done(self, story_id: str) -> bool:
        behaviours = self.story_behaviours(story_id)
        return bool(behaviours) and all(b.state == BehaviourState.DONE for b in behaviours)

    def story_how_to_try(self, story_id: str) -> str:
        """How the Owner runs what this story delivered: the latest commands
        a builder gave inside it. Empty when nothing runnable was reported —
        which the Interpreter must chase, never paper over."""
        for b in reversed(self.story_behaviours(story_id)):
            if b.how_to_run:
                return b.how_to_run
        return ""

    def how_to_try(self, iteration_id: str) -> str:
        """How the owner runs the increment: the INT behaviour's instructions,
        else the latest any builder provided."""
        behaviours = self.iteration_behaviours(iteration_id)
        int_id = self.iterations[iteration_id].int_behaviour_id
        int_b = self.behaviours.get(int_id)
        if int_b is not None and int_b.how_to_run:
            return int_b.how_to_run
        for b in reversed(behaviours):
            if b.how_to_run:
                return b.how_to_run
        return ""

    def story_char_done(self, story_id: str) -> bool:
        """Does this story have a completed characterization behaviour?"""
        return any(
            b.kind == "characterization" and b.state == BehaviourState.DONE
            for b in self.story_behaviours(story_id)
        )
