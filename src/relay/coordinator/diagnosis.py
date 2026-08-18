"""'What is the swarm waiting on?' — answered by a fold, never by a model.

The ubi-es freeze taught the lesson twice: an owner decision sat unanswered
for hours with nothing radiating it, and when asked, the Interpreter went
log-spelunking and confabulated a wrong root cause — while the true answer
was one deterministic fold away. This module IS that fold. `relay status`,
`relay watch`, and the Interpreter's own diagnosis tool all print it; none
of them infer anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from relay.coordinator.model import BehaviourState, SwarmState

# who each non-terminal behaviour state is waiting on
STATE_WAITS_ON: dict[BehaviourState, str] = {
    BehaviourState.SPEC_DISPATCHED: "specifier",
    BehaviourState.RED_PENDING: "toolgate",
    BehaviourState.SATISFIED_PENDING: "toolgate",
    BehaviourState.BUILD_DISPATCHED: "builder",
    BehaviourState.AT_RUN_PENDING: "toolgate",
    BehaviourState.GATES_PENDING: "gates",          # refined per unanswered gate
    BehaviourState.ACCEPTANCE_PENDING: "specifier",
    # everything else is the coordinator's move — stuck there means the
    # coordinator itself is down or wrong, and that must be visible too
    BehaviourState.PLANNED: "coordinator",
    BehaviourState.SPEC_READY: "coordinator",
    BehaviourState.RED_FAILED: "coordinator",
    BehaviourState.RED_VERIFIED: "coordinator",
    BehaviourState.SATISFIED_CLAIMED: "coordinator",
    BehaviourState.BUILT: "coordinator",
    BehaviourState.AT_RED: "coordinator",
    BehaviourState.AT_GREEN: "coordinator",
    BehaviourState.GATES_PASSED: "coordinator",
}

GATE_ROLE: dict[str, str] = {
    "code_review": "reviewer", "test_design": "qa",
    "mutation": "qa", "security": "security",
}


@dataclass(frozen=True)
class WaitingItem:
    subject_id: str
    waiting_on: str      # a role, or "OWNER"
    since: str           # ISO ts
    detail: str

    def age_s(self, now_s: float) -> float:
        return max(0.0, now_s - ts_epoch(self.since))


def ts_epoch(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return datetime.now(UTC).timestamp()


def waiting_on(state: SwarmState) -> list[WaitingItem]:
    items: list[WaitingItem] = []

    # 1. the humans first: open escalations are work the OWNER owes the swarm
    for info in state.decisions.values():
        if not info.closed:
            items.append(WaitingItem(
                subject_id=info.subject_id, waiting_on="OWNER", since=info.since,
                detail=f"decision open (asked {info.asks}x): {info.reason[:140]}",
            ))
    open_subjects = {i.subject_id for i in items}

    # a BLOCKED behaviour with no open ask should be impossible — show it
    # loudly rather than trust that it is
    for b in state.behaviours.values():
        if b.state == BehaviourState.BLOCKED and b.id not in open_subjects:
            items.append(WaitingItem(
                subject_id=b.id, waiting_on="OWNER", since=b.state_since,
                detail="blocked with NO open decision — will be re-asked",
            ))

    # 2. plan mode: an approved iteration with no approved change plan
    for iteration in state.iterations.values():
        if iteration.started and not iteration.aborted and iteration.plan_nudged \
                and iteration.plan_path is None:
            items.append(WaitingItem(
                subject_id=iteration.id, waiting_on="OWNER", since="",
                detail="awaiting change plan — run `relay plan`",
            ))

    # 3. in-flight work, by who owes the next move. PLANNED is queue, not
    # stuckness, and future iterations aren't even queue yet — reporting
    # either as 'waiting' would bury the real signal in noise.
    for b in state.behaviours.values():
        if b.state in (BehaviourState.DONE, BehaviourState.BLOCKED, BehaviourState.PLANNED):
            continue
        parent = state.iterations.get(b.iteration_id)
        if parent is None or not parent.started or parent.aborted:
            continue
        role = STATE_WAITS_ON.get(b.state, "coordinator")
        if b.state == BehaviourState.GATES_PENDING:
            unanswered = [g for g in b.pending_gates.values() if g.verdict is None]
            for g in unanswered:
                items.append(WaitingItem(
                    subject_id=b.id, waiting_on=GATE_ROLE.get(g.gate, g.gate),
                    since=g.since or b.state_since, detail=f"gate {g.gate} unanswered",
                ))
            continue
        items.append(WaitingItem(
            subject_id=b.id, waiting_on=role, since=b.state_since,
            detail=f"state {b.state}"
                   + (f" (re-dispatched {b.same_state_dispatches}x)"
                      if b.same_state_dispatches else ""),
        ))

    # 4. story mutation runs and story/iteration gates
    for story in state.stories.values():
        if story.mutation_run_id:
            run = state.runs.get(story.mutation_run_id)
            if run is not None and run.exit_code is None:
                items.append(WaitingItem(
                    subject_id=story.id, waiting_on="toolgate",
                    since=run.since, detail="mutation run in flight",
                ))
        for g in story.pending_gates.values():
            if g.verdict is None:
                items.append(WaitingItem(
                    subject_id=story.id, waiting_on=GATE_ROLE.get(g.gate, g.gate),
                    since=g.since, detail=f"gate {g.gate} unanswered",
                ))
    for iteration in state.iterations.values():
        for g in iteration.pending_gates.values():
            if g.verdict is None:
                items.append(WaitingItem(
                    subject_id=iteration.id, waiting_on=GATE_ROLE.get(g.gate, g.gate),
                    since=g.since, detail=f"gate {g.gate} unanswered",
                ))
    return items


def render(state: SwarmState, now_s: float) -> str:
    """One skimmable report; '' when nothing is waiting."""
    items = waiting_on(state)
    if not items:
        return ""
    lines = ["WAITING ON"]
    for item in sorted(items, key=lambda i: (i.waiting_on != "OWNER", -i.age_s(now_s))):
        age = int(item.age_s(now_s))
        mins, hours = (age // 60) % 60, age // 3600
        age_txt = f"{hours}h{mins:02d}m" if hours else f"{mins}m"
        marker = "⚠ " if item.waiting_on == "OWNER" else "· "
        lines.append(f"{marker}{item.waiting_on:<10} {item.subject_id:<12} "
                     f"{age_txt:>6}  {item.detail}")
    return "\n".join(lines)
