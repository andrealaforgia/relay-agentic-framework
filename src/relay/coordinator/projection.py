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


def apply(state: SwarmState, env: Envelope) -> SwarmState:
    if env.seq is not None:
        state.last_seq = env.seq
    state.last_event_id = env.event_id

    handler = _HANDLERS.get(env.type)
    if handler is not None:
        handler(state, env)
    return state


def _kind_of(behaviour_id: str) -> str:
    if behaviour_id.endswith(".INT"):
        return "integration"
    if behaviour_id.rsplit(".", 1)[-1].startswith("CHAR"):
        return "characterization"
    return "ac"


# ── roadmap / iterations ─────────────────────────────────────────────────────

def _roadmap_committed(state: SwarmState, env: Envelope) -> None:
    """Latest roadmap wins. Behaviours already DONE survive a re-plan; every
    other behaviour is rebuilt from the new roadmap."""
    done = {bid: b for bid, b in state.behaviours.items() if b.state == BehaviourState.DONE}
    state.iterations.clear()
    state.stories.clear()
    state.behaviours.clear()
    state.behaviour_order.clear()
    state.roadmap_wrote_integration.clear()
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
                if str(ac["id"]).endswith(".INT"):
                    # code makes these; the roadmap check reports it loudly
                    state.roadmap_wrote_integration.append(str(ac["id"]))
                    continue
                behaviour = done.get(ac["id"]) or Behaviour(
                    id=ac["id"],
                    iteration_id=iteration.id,
                    story_id=story.id,
                    kind=_kind_of(ac["id"]),
                    ac_text=ac["text"],
                    title=str(ac.get("title", "")),
                )
                state.behaviours[behaviour.id] = behaviour
                if behaviour.id not in state.behaviour_order:
                    state.behaviour_order.append(behaviour.id)
                story.behaviour_ids.append(behaviour.id)
            # Every story ends with its own integration behaviour, created by
            # code so it cannot be forgotten or written by a model. A story is
            # a vertical slice or it is not a story: this is where the Owner
            # gets something to try, without waiting for the iteration.
            _add_integration(
                state, story.behaviour_ids, iteration.id, f"{story.id}.INT",
                story_id=story.id,
                ac_text=(f"Story {story.id} works end to end through the product's real "
                         f"surface: {story.title}"),
                title=f"{story.title} — working end to end"[:80],
                done=done,
            )
            story.int_behaviour_id = f"{story.id}.INT"
        # and the iteration's own, which proves the stories work TOGETHER
        int_id = f"{iteration.id}.INT"
        _add_integration(
            state, None, iteration.id, int_id, story_id=None,
            ac_text=(f"The integrated increment of {iteration.id} is demonstrable end to "
                     f"end: {iteration.increment}"),
            title=f"{iteration.increment} — working end to end"[:80],
            done=done,
        )
        iteration.int_behaviour_id = int_id


def _add_integration(
    state: SwarmState,
    belongs_to: list[str] | None,
    iteration_id: str,
    int_id: str,
    *,
    story_id: str | None,
    ac_text: str,
    title: str,
    done: dict[str, Behaviour],
) -> None:
    """Create an integration behaviour. Never authored by a model: the roadmap
    is validated to reject `.INT` ids precisely so this stays code's job."""
    behaviour = done.get(int_id) or Behaviour(
        id=int_id, iteration_id=iteration_id, story_id=story_id,
        kind="integration", ac_text=ac_text, title=title,
    )
    state.behaviours[int_id] = behaviour
    if int_id not in state.behaviour_order:
        state.behaviour_order.append(int_id)
    if belongs_to is not None and int_id not in belongs_to:
        belongs_to.append(int_id)


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


def _plan_committed(state: SwarmState, env: Envelope) -> None:
    iteration = state.iterations.get(str(env.payload["iteration_id"]))
    if iteration:
        iteration.plan_path = str(env.payload["plan_path"])


def _stall_detected(state: SwarmState, env: Envelope) -> None:
    # the coordinator's own plan-mode nudge: fold it so a restart never re-nags
    if str(env.payload.get("waiting_on")) == "planner":
        iteration = state.iterations.get(str(env.payload.get("subject_id")))
        if iteration:
            iteration.plan_nudged = True


def _pr_approved(state: SwarmState, env: Envelope) -> None:
    iteration = state.iterations.get(str(env.payload["iteration_id"]))
    if iteration:
        iteration.pr_approved = True


def _pr_opened(state: SwarmState, env: Envelope) -> None:
    iteration = state.iterations.get(str(env.payload["iteration_id"]))
    if iteration:
        iteration.pr_opened = True


# ── recon / legacy intake ────────────────────────────────────────────────────

def _recon_requested(state: SwarmState, env: Envelope) -> None:
    state.recon_requested = True


def _recon_report(state: SwarmState, env: Envelope) -> None:
    state.recon_done = True
    state.risk_areas = [str(p) for p in env.payload.get("risk_areas", [])]


# ── behaviour lifecycle ──────────────────────────────────────────────────────

def _behaviour(state: SwarmState, env: Envelope) -> Behaviour | None:
    bid = env.payload.get("behaviour_id") or env.behaviour_id
    return state.behaviours.get(str(bid)) if bid else None


def _batched(state: SwarmState, env: Envelope, field: str) -> list[Behaviour]:
    """Every behaviour one dispatch covers.

    At story granularity a single request carries the whole story, and state
    is a fold over the ledger (D3) — so the fold must move ALL of them, not
    just the one named at the top of the payload. Setting the rest in the
    dispatcher's memory alone made a restart re-dispatch finished work.
    """
    items = env.payload.get(field)
    if not isinstance(items, list):
        anchor = _behaviour(state, env)
        return [anchor] if anchor else []
    out = []
    for item in items:
        if isinstance(item, dict):
            b = state.behaviours.get(str(item.get("behaviour_id")))
            if b is not None:
                out.append(b)
    return out


def _spec_requested(state: SwarmState, env: Envelope) -> None:
    for b in _batched(state, env, "criteria"):
        b.state = BehaviourState.SPEC_DISPATCHED
        b.base_sha = str(env.payload["base_sha"])
        b.spec_attempts += 1


def _spec_ready(state: SwarmState, env: Envelope) -> None:
    b = _behaviour(state, env)
    if b and b.state == BehaviourState.SPEC_DISPATCHED:
        b.state = BehaviourState.SPEC_READY
        b.test_paths = list(env.payload["test_paths"])
        b.touches = list(env.payload.get("touches", []))
        b.spec_commit = str(env.payload["commit_sha"])


def _spec_satisfied(state: SwarmState, env: Envelope) -> None:
    """The specifier declares the criterion already holds: the committed test
    is a guard, and the toolgate must still prove it green."""
    b = _behaviour(state, env)
    if b and b.state == BehaviourState.SPEC_DISPATCHED:
        b.state = BehaviourState.SATISFIED_CLAIMED
        b.test_paths = list(env.payload["test_paths"])
        b.spec_commit = str(env.payload["commit_sha"])


def _error_raised(state: SwarmState, env: Envelope) -> None:
    """An assistant reported it is stuck: this must never vanish."""
    detail = str(env.payload.get("detail", env.payload.get("kind", "error")))
    b = _behaviour(state, env)
    if b is None:
        state.unescalated_errors[env.event_id] = f"{env.from_role}: {detail}"
    elif str(env.payload.get("kind")) == "spec_conflict":
        # not a stuck assistant: an existing acceptance test contradicts this
        # behaviour, which only the specifier may resolve
        b.spec_conflict = detail
    else:
        b.error_reported = detail


def _decision_requested(state: SwarmState, env: Envelope) -> None:
    _owner_decision_needed(state, env)
    source = env.payload.get("source_event_id")
    if source:
        state.unescalated_errors.pop(str(source), None)


def _run_requested(state: SwarmState, env: Envelope) -> None:
    run_id = str(env.payload["run_id"])
    kind = str(env.payload["kind"])
    if kind == "mutation":
        story = state.stories.get(str(env.story_id)) if env.story_id else None
        if story is not None:
            story.mutation_run_id = run_id
            state.runs[run_id] = RunInfo(
                run_id=run_id, purpose=RunPurpose.MUTATION, story_id=story.id
            )
        return
    if kind != "acceptance_test":
        return
    b = _behaviour(state, env)
    if b is None:
        return
    if b.state == BehaviourState.SPEC_READY:
        purpose = RunPurpose.RED_VERIFICATION
        b.state = BehaviourState.RED_PENDING
    elif b.state == BehaviourState.BUILT:
        purpose = RunPurpose.AT_GREEN
        b.state = BehaviourState.AT_RUN_PENDING
    elif b.state == BehaviourState.SATISFIED_CLAIMED:
        purpose = RunPurpose.SATISFIED_CHECK
        b.state = BehaviourState.SATISFIED_PENDING
    else:
        return
    state.runs[run_id] = RunInfo(run_id=run_id, purpose=purpose, behaviour_id=b.id)


def _run_completed(state: SwarmState, env: Envelope) -> None:
    run = state.runs.get(str(env.payload["run_id"]))
    if run is None:
        return
    run.exit_code = int(env.payload["exit_code"])
    if run.purpose == RunPurpose.MUTATION:
        return  # evidence only; qa judges via the gate
    b = state.behaviours.get(run.behaviour_id or "")
    if b is None:
        return
    if run.purpose == RunPurpose.RED_VERIFICATION and b.state == BehaviourState.RED_PENDING:
        # An 'ac' spec must FAIL before build; a characterization spec pins
        # current behaviour, so it must PASS (inverted).
        #
        # An integration spec composes behaviours that are already delivered,
        # so it is allowed to be green the moment it is written — that IS the
        # composition holding, and demanding red first makes a clean
        # composition indistinguishable from a broken one. Green means done;
        # red means a real integration gap, which the builder then closes.
        if b.kind == "integration" and run.exit_code == 0:
            b.state = BehaviourState.DONE
            b.built_commit = b.spec_commit
            return
        expected_fail = b.kind != "characterization"
        verified = (run.exit_code != 0) if expected_fail else (run.exit_code == 0)
        b.state = BehaviourState.RED_VERIFIED if verified else BehaviourState.RED_FAILED
        if b.state == BehaviourState.RED_FAILED:
            b.last_fail_reason = (
                "red verification: the new acceptance test already passes"
                if expected_fail else
                "characterization test does not pass against current behaviour"
            )
    elif run.purpose == RunPurpose.AT_GREEN and b.state == BehaviourState.AT_RUN_PENDING:
        if run.exit_code == 0:
            b.state = BehaviourState.AT_GREEN
        else:
            b.state = BehaviourState.AT_RED
            b.last_fail_reason = "acceptance test still failing after build"
    elif run.purpose == RunPurpose.SATISFIED_CHECK and b.state == BehaviourState.SATISFIED_PENDING:
        if run.exit_code == 0:
            # criterion machine-verified as already met; the guard test stands
            b.state = BehaviourState.DONE
            b.built_commit = b.spec_commit
        else:
            b.state = BehaviourState.RED_FAILED
            b.last_fail_reason = "claimed already-satisfied, but the guard test fails"


def _build_requested(state: SwarmState, env: Envelope) -> None:
    for b in _batched(state, env, "behaviours"):
        if b.state == BehaviourState.RED_VERIFIED:
            b.state = BehaviourState.BUILD_DISPATCHED


def _decision_made(state: SwarmState, env: Envelope) -> None:
    """The Owner's answer to an escalation, which is the way OUT of BLOCKED.

    Without this the coordinator escalates, the Owner answers, and nothing
    consumes it: a blocked behaviour stays blocked for ever. `retry` puts the
    behaviour back at the start of its cycle with a fresh attempt budget;
    `drop` accepts that it will not be delivered in this iteration.
    """
    if env.to_role != "coordinator":
        return
    subject = str(env.payload.get("subject_id") or env.behaviour_id or "")
    b = state.behaviours.get(subject)
    if b is None or b.state != BehaviourState.BLOCKED:
        return
    decision = str(env.payload.get("decision"))
    if decision == "retry":
        b.state = BehaviourState.PLANNED
        b.attempt = 1
        b.spec_attempts = 0
        b.pending_gates.clear()
        b.last_fail_reason = None
    elif decision == "drop":
        b.state = BehaviourState.DONE       # not delivered, but no longer in the way
        b.last_fail_reason = "dropped by the Owner"


def _rework_requested(state: SwarmState, env: Envelope) -> None:
    b = _behaviour(state, env)
    if b:
        # rework goes to whoever can act on it, and the state must name that
        # role: test_design/mutation findings are the specifier's, and parking
        # such a behaviour in BUILD_DISPATCHED makes the specifier's answering
        # spec.written unmatchable, so the behaviour waits forever for a build
        # nobody was asked for.
        b.state = (BehaviourState.SPEC_DISPATCHED if env.to_role == "specifier"
                   else BehaviourState.BUILD_DISPATCHED)
        b.attempt = int(env.payload["attempt"])
        b.pending_gates.clear()
        # a story-level gate failure loops back through this behaviour: the
        # story must re-earn its gates on the next completion
        if b.story_id and b.story_id in state.stories:
            state.stories[b.story_id].reset_gates()


def _built(state: SwarmState, env: Envelope) -> None:
    b = _behaviour(state, env)
    if b and b.state == BehaviourState.BUILD_DISPATCHED:
        b.state = BehaviourState.BUILT
        b.built_commit = str(env.payload["commit_sha"])
        if env.payload.get("how_to_run"):
            b.how_to_run = str(env.payload["how_to_run"])


def _gate_requested(state: SwarmState, env: Envelope) -> None:
    subject_kind = str(env.payload["subject_kind"])
    subject_id = str(env.payload["subject_id"])
    gate_id = str(env.payload["gate_id"])
    info = GateInfo(gate_id=gate_id, gate=str(env.payload["gate"]), subject_id=subject_id)
    if subject_kind == "behaviour":
        b = state.behaviours.get(subject_id)
        if b is not None:
            b.pending_gates[gate_id] = info
            if b.state == BehaviourState.AT_GREEN:
                b.state = BehaviourState.GATES_PENDING
    elif subject_kind == "story":
        story = state.stories.get(subject_id)
        if story is not None:
            story.pending_gates[gate_id] = info
    elif subject_kind == "iteration":
        iteration = state.iterations.get(subject_id)
        if iteration is not None:
            iteration.pending_gates[gate_id] = info


def _gate_verdict(state: SwarmState, env: Envelope) -> None:
    gate_id = str(env.payload["gate_id"])
    verdict = str(env.payload["verdict"])
    for b in state.behaviours.values():
        gate = b.pending_gates.get(gate_id)
        if gate is not None:
            gate.verdict = verdict
            if b.state == BehaviourState.GATES_PENDING:
                verdicts = [g.verdict for g in b.pending_gates.values()]
                if any(v == "fail" for v in verdicts):
                    b.state = BehaviourState.AT_RED
                    b.last_fail_reason = f"gate {gate.gate} failed"
                    if verdict == "fail":
                        b.last_fail_gate = gate.gate
                        found = env.payload.get("findings")
                        b.last_findings = list(found) if isinstance(found, list) else []
                elif all(v == "pass" for v in verdicts):
                    b.state = BehaviourState.GATES_PASSED
            return
    for story in state.stories.values():
        gate = story.pending_gates.get(gate_id)
        if gate is not None:
            gate.verdict = verdict
            return
    for iteration in state.iterations.values():
        gate = iteration.pending_gates.get(gate_id)
        if gate is not None:
            gate.verdict = verdict
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
        b.state = BehaviourState.AT_RED
        b.last_fail_reason = str(env.payload.get("reason") or "acceptance judgement failed")


def _owner_decision_needed(state: SwarmState, env: Envelope) -> None:
    subject_id = str(env.payload["subject_id"])
    b = state.behaviours.get(subject_id)
    if b is not None:
        b.state = BehaviourState.BLOCKED
        return
    story = state.stories.get(subject_id)
    if story is not None:
        story.escalated = True
        return
    iteration = state.iterations.get(subject_id)
    if iteration is not None:
        iteration.escalated = True


def _progress_announced(state: SwarmState, env: Envelope) -> None:
    state.last_progress = (str(env.payload["iteration_id"]), int(env.payload["behaviours_done"]))


_HANDLERS = {
    "roadmap.committed": _roadmap_committed,
    "roadmap.rejected": _roadmap_rejected,
    "iteration.started": _iteration_started,
    "iteration.aborted": _iteration_aborted,
    "story.completed": _story_done_announced,
    "iteration.finished": _iteration_ready_announced,
    "decision.requested": _decision_requested,
    "plan.committed": _plan_committed,
    "stall.detected": _stall_detected,
    "pr.approved": _pr_approved,
    "pr.opened": _pr_opened,
    "recon.requested": _recon_requested,
    "recon.completed": _recon_report,
    "spec.requested": _spec_requested,
    "spec.written": _spec_ready,
    "spec.satisfied": _spec_satisfied,
    "error.raised": _error_raised,
    "decision.made": _decision_made,
    "build.requested": _build_requested,
    "rework.requested": _rework_requested,
    "behaviour.built": _built,
    "judgement.requested": _judgement_requested,
    "acceptance.judged": _acceptance_verdict,
    "run.requested": _run_requested,
    "run.completed": _run_completed,
    "gate.requested": _gate_requested,
    "gate.judged": _gate_verdict,
    "progress.reported": _progress_announced,
}
