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
    RED_FAILED = "red_failed"            # the "failing" test passed — back to specifier
    RED_VERIFIED = "red_verified"
    BUILD_DISPATCHED = "build_dispatched"
    BUILT = "built"
    AT_RUN_PENDING = "at_run_pending"    # post-build AT run dispatched to toolgate
    AT_RED = "at_red"                    # AT still failing after build — rework
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


@dataclass
class RunInfo:
    run_id: str
    purpose: RunPurpose
    behaviour_id: str
    exit_code: int | None = None


@dataclass
class GateInfo:
    gate_id: str
    gate: str
    subject_id: str
    verdict: str | None = None  # pass | fail


@dataclass
class Behaviour:
    id: str
    iteration_id: str
    story_id: str | None            # None for INT behaviours
    kind: str                        # ac | integration | characterization
    ac_text: str
    state: BehaviourState = BehaviourState.PLANNED
    attempt: int = 1
    test_paths: list[str] = field(default_factory=list)
    touches: list[str] = field(default_factory=list)
    spec_commit: str | None = None
    built_commit: str | None = None
    pending_gates: dict[str, GateInfo] = field(default_factory=dict)
    last_fail_reason: str | None = None


@dataclass
class Story:
    id: str
    iteration_id: str
    title: str
    behaviour_ids: list[str] = field(default_factory=list)
    done_announced: bool = False     # plan.story_done seen on the ledger


@dataclass
class Iteration:
    id: str
    goal: str
    increment: str
    story_ids: list[str] = field(default_factory=list)
    int_behaviour_id: str = ""
    started: bool = False
    ready_announced: bool = False    # plan.iteration_ready seen on the ledger
    aborted: bool = False


@dataclass
class SwarmState:
    """The projection. `last_seq` makes snapshots verifiable."""

    last_seq: int = 0
    last_event_id: str | None = None
    roadmap_committed: bool = False
    intake_mode: str = "greenfield"
    iterations: dict[str, Iteration] = field(default_factory=dict)
    stories: dict[str, Story] = field(default_factory=dict)
    behaviours: dict[str, Behaviour] = field(default_factory=dict)
    runs: dict[str, RunInfo] = field(default_factory=dict)
    # ordering as authored in the roadmap; INT behaviours appended last per iteration
    behaviour_order: list[str] = field(default_factory=list)

    def iteration_behaviours(self, iteration_id: str) -> list[Behaviour]:
        return [
            self.behaviours[bid]
            for bid in self.behaviour_order
            if self.behaviours[bid].iteration_id == iteration_id
        ]

    def story_behaviours(self, story_id: str) -> list[Behaviour]:
        return [
            self.behaviours[bid]
            for bid in self.stories[story_id].behaviour_ids
        ]

    def active_iteration(self) -> Iteration | None:
        for iteration in self.iterations.values():
            if iteration.started and not iteration.aborted and not self._iteration_done(iteration):
                return iteration
        return None

    def _iteration_done(self, iteration: Iteration) -> bool:
        behaviours = self.iteration_behaviours(iteration.id)
        return bool(behaviours) and all(b.state == BehaviourState.DONE for b in behaviours)

    def iteration_done(self, iteration_id: str) -> bool:
        return self._iteration_done(self.iterations[iteration_id])

    def story_done(self, story_id: str) -> bool:
        behaviours = self.story_behaviours(story_id)
        return bool(behaviours) and all(b.state == BehaviourState.DONE for b in behaviours)
