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
    PROPERTIES = "properties"
    SETUP = "setup"


@dataclass
class RunInfo:
    run_id: str
    purpose: RunPurpose
    behaviour_id: str | None = None
    story_id: str | None = None
    exit_code: int | None = None
    summary: str = ""                    # tail of the run output (findings need it)
    since: str = ""                      # dispatch ts — deadline supervision
    # set when the command did not run at all: the exit code is then evidence
    # about the machine, never about the code, and nothing may be read into it
    fault: str = ""


@dataclass
class GateInfo:
    gate_id: str
    gate: str            # code_review | test_design | mutation | security
    subject_id: str
    # pass | fail | contested — contested is the projection's refusal to
    # honor a pass that flipped on identical code or left prior findings
    # undispositioned. It never resolves silently: the Owner decides.
    verdict: str | None = None
    since: str = ""              # dispatch ts — deadline supervision needs it
    attempt: int = 0             # times this gate was re-dispatched (folded)
    commit_sha: str = ""         # the code this gate was asked to judge
    contested_reason: str = ""


@dataclass
class DecisionInfo:
    """An open escalation: the swarm is waiting on the OWNER. Folded from
    decision.requested; closed by a decision.made that actually matched.
    The whole point of tracking these is that 'waiting on a human' must be
    as diagnosable and as supervised as waiting on any worker."""

    gate_id: str
    subject_id: str
    reason: str
    since: str
    last_ask: str        # when the Owner was last asked (nudges update this)
    asks: int = 1
    closed: bool = False


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
    state_since: str = ""                # ts of the last state change OR re-dispatch
    same_state_dispatches: int = 0       # re-dispatches without a state change
    # the state as of the last FOLDED event — the dispatcher mirrors its own
    # publishes in memory, so the fold must diff against what the ledger last
    # said, or every dispatch echo would look like a re-dispatch
    folded_state: str = ""
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
    # an infrastructure fault seen on one of this behaviour's runs, awaiting
    # escalation. Cleared by the Owner's retry — the toolchain is fixed by a
    # human, and no number of attempts will make a missing binary appear.
    infra_fault: str | None = None


@dataclass
class Story:
    id: str
    iteration_id: str
    title: str
    behaviour_ids: list[str] = field(default_factory=list)
    int_behaviour_id: str = ""          # the story's own end-to-end behaviour
    done_announced: bool = False        # story.completed seen on the ledger
    mutation_run_id: str | None = None  # in-flight or judged mutation run
    properties_run_id: str | None = None  # in-flight or judged property-suite run
    pending_gates: dict[str, GateInfo] = field(default_factory=dict)
    escalated: bool = False
    gates_waived: bool = False          # the Owner's `drop`: proceed despite the gate
    fix_requested: bool = False         # the Owner's `fix`: findings become rework

    def gates_passed(self) -> bool:
        if self.gates_waived:
            return True
        return bool(self.pending_gates) and all(
            g.verdict == "pass" for g in self.pending_gates.values()
        )

    def gates_failed(self) -> bool:
        return any(g.verdict == "fail" for g in self.pending_gates.values())

    def reset_gates(self) -> None:
        self.mutation_run_id = None
        self.properties_run_id = None
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
    # the toolchain the approved change plan binds this iteration to, by run
    # kind: acceptance_test | suite | mutation | properties
    commands: dict[str, str] = field(default_factory=dict)
    plan_nudged: bool = False           # plan.requested dispatched to the planner
    plan_requested_since: str = ""      # when — supervised like any dispatch
    plan_redispatched: bool = False     # the one supervision re-ask happened
    plan_drafted: bool = False          # draft is with the Owner in chat
    # the plan's `setup` command proven at plan time: no behaviour is
    # dispatched until the toolchain has actually bootstrapped once
    setup_run_id: str | None = None
    # greenfield bootstrap: a failed setup proof on a from-scratch repo sends
    # the BUILDER to initialise the project before anyone bothers the Owner
    scaffold_dispatched: bool = False   # mirror latch (same-tick re-dispatch guard)
    scaffold_requested_since: str = ""  # fold-set; supervision clock
    scaffold_redispatched: bool = False
    scaffold_done: bool = False
    gates_waived: bool = False          # the Owner's `drop`: proceed despite the gate
    fix_requested: bool = False         # the Owner's `fix`: findings become rework
    properties_run_id: str | None = None  # in-flight or judged property-suite run

    def gates_passed(self) -> bool:
        if self.gates_waived:
            return True
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
    # every escalation ever asked, by gate_id — open ones are what the swarm
    # is waiting on the OWNER for; closed ones make duplicates idempotent
    decisions: dict[str, DecisionInfo] = field(default_factory=dict)
    # the findings ratchet: once a gate finds something on a subject, it stays
    # attached (keyed "subject|gate") until a verdict dispositions it — fresh
    # eyes can forget, the fold cannot. Each entry carries found_at (sha).
    open_findings: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    # a decision.made arrived that matched nothing open: re-ask immediately
    decision_mismatch: bool = False
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
