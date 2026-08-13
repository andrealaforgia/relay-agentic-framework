"""fold(events) -> SwarmState.

`apply` handles every event that affects work state — including the
coordinator's OWN dispatch events. That is what makes resume exact: a
restarted coordinator replays the ledger and sees precisely which dispatches
it already issued, so it can never double-dispatch. Events that don't affect
work state (chat conversation, control corrections, system noise) are
deliberately ignored here.
"""

from __future__ import annotations

from collections.abc import Iterable

from relay.contract.envelope import Envelope
from relay.coordinator.model import (
    Behaviour,
    BehaviourState,
    GateInfo,
    Iteration,
    RunInfo,
    RunPurpose,
    Story,
    SwarmState,
)


def project(events: Iterable[Envelope]) -> SwarmState:
    state = SwarmState()
    for env in events:
        apply(state, env)
    return state


def apply(state: SwarmState, env: Envelope) -> SwarmState:  # noqa: C901 — one dispatch table
    if env.seq is not None:
        state.last_seq = env.seq
    state.last_event_id = env.event_id

    handler = _HANDLERS.get(env.type)
    if handler is not None:
        handler(state, env)
    return state


# ── roadmap / iterations ─────────────────────────────────────────────────────

def _roadmap_committed(state: SwarmState, env: Envelope) -> None:
    """Latest roadmap wins. Behaviours already DONE survive a re-plan; every
    other behaviour is rebuilt from the new roadmap."""
    done = {bid: b for bid, b in state.behaviours.items() if b.state == BehaviourState.DONE}
    state.iterations.clear()
    state.stories.clear()
    state.behaviours.clear()
    state.behaviour_order.clear()
    state.roadmap_committed = True
    state.intake_mode = str(env.payload["intake"]["mode"])

    for it in env.payload["roadmap"]["iterations"]:
        iteration = Iteration(id=it["id"], goal=it["goal"], increment=it["increment"])
        state.iterations[iteration.id] = iteration
        for st in it["stories"]:
            story = Story(id=st["id"], iteration_id=iteration.id, title=st["title"])
            state.stories[story.id] = story
            iteration.story_ids.append(story.id)
            for ac in st["acceptance_criteria"]:
                behaviour = done.get(ac["id"]) or Behaviour(
                    id=ac["id"],
                    iteration_id=iteration.id,
                    story_id=story.id,
                    kind="ac",
                    ac_text=ac["text"],
                )
                state.behaviours[behaviour.id] = behaviour
                state.behaviour_order.append(behaviour.id)
                story.behaviour_ids.append(behaviour.id)
        # The mandatory iteration-level integration behaviour — created by
        # code, so it cannot be forgotten (DECISIONS.md, lesson 4).
        int_id = f"{iteration.id}.INT"
        int_behaviour = done.get(int_id) or Behaviour(
            id=int_id,
            iteration_id=iteration.id,
            story_id=None,
            kind="integration",
            ac_text=(
                f"The integrated increment of {iteration.id} is demonstrable end to end: "
                f"{iteration.increment}"
            ),
        )
        iteration.int_behaviour_id = int_id
        state.behaviours[int_id] = int_behaviour
        state.behaviour_order.append(int_id)


def _roadmap_rejected(state: SwarmState, env: Envelope) -> None:
    state.roadmap_committed = False


def _iteration_started(state: SwarmState, env: Envelope) -> None:
    iteration = state.iterations.get(str(env.payload["iteration_id"]))
    if iteration:
        iteration.started = True


def _iteration_aborted(state: SwarmState, env: Envelope) -> None:
    iteration = state.iterations.get(str(env.payload["iteration_id"]))
    if iteration:
        iteration.aborted = True


def _story_done_announced(state: SwarmState, env: Envelope) -> None:
    story = state.stories.get(str(env.payload["story_id"]))
    if story:
        story.done_announced = True


def _iteration_ready_announced(state: SwarmState, env: Envelope) -> None:
    iteration = state.iterations.get(str(env.payload["iteration_id"]))
    if iteration:
        iteration.ready_announced = True


# ── behaviour lifecycle ──────────────────────────────────────────────────────

def _behaviour(state: SwarmState, env: Envelope) -> Behaviour | None:
    bid = env.payload.get("behaviour_id") or env.behaviour_id
    return state.behaviours.get(str(bid)) if bid else None


def _spec_requested(state: SwarmState, env: Envelope) -> None:
    b = _behaviour(state, env)
    if b:
        b.state = BehaviourState.SPEC_DISPATCHED


def _spec_ready(state: SwarmState, env: Envelope) -> None:
    b = _behaviour(state, env)
    if b and b.state == BehaviourState.SPEC_DISPATCHED:
        b.state = BehaviourState.SPEC_READY
        b.test_paths = list(env.payload["test_paths"])
        b.touches = list(env.payload.get("touches", []))
        b.spec_commit = str(env.payload["commit_sha"])


def _run_requested(state: SwarmState, env: Envelope) -> None:
    b = _behaviour(state, env)
    if b is None:
        return
    run_id = str(env.payload["run_id"])
    if b.state == BehaviourState.SPEC_READY:
        purpose = RunPurpose.RED_VERIFICATION
        b.state = BehaviourState.RED_PENDING
    elif b.state == BehaviourState.BUILT:
        purpose = RunPurpose.AT_GREEN
        b.state = BehaviourState.AT_RUN_PENDING
    else:
        return
    state.runs[run_id] = RunInfo(run_id=run_id, purpose=purpose, behaviour_id=b.id)


def _run_completed(state: SwarmState, env: Envelope) -> None:
    run = state.runs.get(str(env.payload["run_id"]))
    if run is None:
        return
    run.exit_code = int(env.payload["exit_code"])
    b = state.behaviours.get(run.behaviour_id)
    if b is None:
        return
    if run.purpose == RunPurpose.RED_VERIFICATION and b.state == BehaviourState.RED_PENDING:
        # a "failing" acceptance test must actually fail before build dispatch
        b.state = BehaviourState.RED_VERIFIED if run.exit_code != 0 else BehaviourState.RED_FAILED
        if b.state == BehaviourState.RED_FAILED:
            b.last_fail_reason = "red verification: the new acceptance test already passes"
    elif run.purpose == RunPurpose.AT_GREEN and b.state == BehaviourState.AT_RUN_PENDING:
        if run.exit_code == 0:
            b.state = BehaviourState.AT_GREEN
        else:
            b.state = BehaviourState.AT_RED
            b.last_fail_reason = "acceptance test still failing after build"


def _build_requested(state: SwarmState, env: Envelope) -> None:
    b = _behaviour(state, env)
    if b and b.state in (BehaviourState.RED_VERIFIED,):
        b.state = BehaviourState.BUILD_DISPATCHED


def _rework_requested(state: SwarmState, env: Envelope) -> None:
    b = _behaviour(state, env)
    if b:
        b.state = BehaviourState.BUILD_DISPATCHED
        b.attempt = int(env.payload["attempt"])


def _built(state: SwarmState, env: Envelope) -> None:
    b = _behaviour(state, env)
    if b and b.state == BehaviourState.BUILD_DISPATCHED:
        b.state = BehaviourState.BUILT
        b.built_commit = str(env.payload["commit_sha"])


def _gate_requested(state: SwarmState, env: Envelope) -> None:
    subject_id = str(env.payload["subject_id"])
    b = state.behaviours.get(subject_id)
    if b is not None and env.payload["subject_kind"] == "behaviour":
        gate_id = str(env.payload["gate_id"])
        b.pending_gates[gate_id] = GateInfo(
            gate_id=gate_id, gate=str(env.payload["gate"]), subject_id=subject_id
        )
        if b.state == BehaviourState.AT_GREEN:
            b.state = BehaviourState.GATES_PENDING


def _gate_verdict(state: SwarmState, env: Envelope) -> None:
    gate_id = str(env.payload["gate_id"])
    for b in state.behaviours.values():
        gate = b.pending_gates.get(gate_id)
        if gate is None:
            continue
        gate.verdict = str(env.payload["verdict"])
        if b.state == BehaviourState.GATES_PENDING:
            verdicts = [g.verdict for g in b.pending_gates.values()]
            if any(v == "fail" for v in verdicts):
                b.state = BehaviourState.AT_RED  # dispatcher turns this into rework
                b.last_fail_reason = f"gate {gate.gate} failed"
            elif all(v == "pass" for v in verdicts):
                b.state = BehaviourState.GATES_PASSED
        return


def _judgement_requested(state: SwarmState, env: Envelope) -> None:
    b = _behaviour(state, env)
    if b and b.state in (BehaviourState.AT_GREEN, BehaviourState.GATES_PASSED):
        b.state = BehaviourState.ACCEPTANCE_PENDING


def _acceptance_verdict(state: SwarmState, env: Envelope) -> None:
    b = _behaviour(state, env)
    if b is None or b.state != BehaviourState.ACCEPTANCE_PENDING:
        return
    if env.payload["verdict"] == "pass":
        b.state = BehaviourState.DONE
        b.pending_gates.clear()
    else:
        b.state = BehaviourState.AT_RED  # dispatcher decides: rework or escalate
        b.last_fail_reason = str(env.payload.get("reason") or "acceptance judgement failed")


def _progress_announced(state: SwarmState, env: Envelope) -> None:
    state.last_progress = (str(env.payload["iteration_id"]), int(env.payload["behaviours_done"]))


def _owner_decision_needed(state: SwarmState, env: Envelope) -> None:
    b = state.behaviours.get(str(env.payload["subject_id"]))
    if b is not None:
        b.state = BehaviourState.BLOCKED


_HANDLERS = {
    "plan.roadmap_committed": _roadmap_committed,
    "plan.roadmap_rejected": _roadmap_rejected,
    "plan.iteration_started": _iteration_started,
    "plan.iteration_aborted": _iteration_aborted,
    "plan.story_done": _story_done_announced,
    "plan.iteration_ready": _iteration_ready_announced,
    "plan.owner_decision_needed": _owner_decision_needed,
    "chat.progress": _progress_announced,
    "work.spec_requested": _spec_requested,
    "work.spec_ready": _spec_ready,
    "work.build_requested": _build_requested,
    "work.rework_requested": _rework_requested,
    "work.built": _built,
    "work.judgement_requested": _judgement_requested,
    "work.acceptance_verdict": _acceptance_verdict,
    "run.requested": _run_requested,
    "run.completed": _run_completed,
    "gate.requested": _gate_requested,
    "gate.verdict": _gate_verdict,
}
