"""Plan mode: no behaviour is dispatched until an Owner-approved change plan
is committed — a dispatcher rule. Planning happens IN `relay chat`: the
coordinator dispatches the planner once, the interpreter relays the draft and
the feedback, and plan.committed unblocks — exact across restarts."""

from __future__ import annotations

from pathlib import Path

from test_coordinator import ROADMAP, MiniSwarm

from relay.coordinator.model import BehaviourState
from relay.coordinator.policy import Policy
from relay.workers import briefing

PLAN_POLICY = Policy(plan_required=True)
SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"


def _start(client, publisher, policy) -> MiniSwarm:
    mini = MiniSwarm(client, publisher, policy=policy)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    mini.pump()
    return mini


def test_plan_gate_blocks_dispatch_and_dispatches_the_planner_once(client, publisher) -> None:
    swarm = _start(client, publisher, PLAN_POLICY)
    assert swarm.sent("spec.requested") == []           # nothing dispatched
    (ask,) = swarm.sent("plan.requested")               # the planner is asked once
    assert ask.to_role == "planner"
    assert ask.payload["iteration_id"] == "I1"
    assert ask.payload["goal"]                          # context travels with the ask
    swarm.pump()
    assert len(swarm.sent("plan.requested")) == 1       # never re-asked by react()

    # cold restart: replay produces no second dispatch and still no spec
    fresh = MiniSwarm(client, publisher, policy=PLAN_POLICY)
    fresh.pump()
    assert len(fresh.sent("plan.requested")) == 1
    assert fresh.sent("spec.requested") == []


def test_plan_committed_unblocks_the_iteration(client, publisher) -> None:
    swarm = _start(client, publisher, PLAN_POLICY)
    publisher.send("planner", "coordinator", "plan.committed",
                   {"iteration_id": "I1", "plan_path": "docs/relay/plans/I1.md",
                    "summary": "extend the existing abstraction", "commit_sha": SHA},
                   iteration_id="I1")
    swarm.pump()
    assert swarm.state.iterations["I1"].plan_path == "docs/relay/plans/I1.md"
    assert len(swarm.sent("spec.requested")) >= 1       # work flows
    assert swarm.behaviour("I1.S1.B1").state == BehaviourState.SPEC_DISPATCHED


def test_plan_mode_off_changes_nothing(client, publisher) -> None:
    swarm = _start(client, publisher, Policy(plan_required=False))
    assert len(swarm.sent("spec.requested")) >= 1
    assert swarm.sent("stall.detected") == []


def test_plan_briefing_binds_specifier_builder_reviewer_not_analyst(tmp_path: Path) -> None:
    plans = tmp_path / "docs" / "relay" / "plans"
    plans.mkdir(parents=True)
    (plans / "I1.md").write_text("Extend PricingRule; booking module untouched.")
    payload = {"behaviour_id": "I1.S1.B1"}
    for role in ("specifier", "builder", "reviewer"):
        text = briefing.build(tmp_path, role, "spec.requested", payload)
        assert "approved change plan for I1" in text and "PricingRule" in text
    assert "PricingRule" not in briefing.build(tmp_path, "analyst", "analysis.requested", payload)
    # no plan file for I2 -> silence, not a crash
    assert briefing.build(tmp_path, "builder", "spec.requested", {"behaviour_id": "I2.S1.B1"}) == ""


def test_knowledge_briefing_slices_per_role(tmp_path: Path) -> None:
    knowledge = tmp_path / "docs" / "relay" / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "invariants.md").write_text("Never round prices before summing.")
    (knowledge / "domain.md").write_text("A 'stay' is one night in one room.")
    specifier = briefing.build(tmp_path, "specifier", "spec.requested", {})
    assert "Never round prices" in specifier and "ground truth" in specifier
    qa = briefing.build(tmp_path, "qa", "gate.requested", {})
    assert "Never round prices" not in qa                # not qa's slice
    assert briefing.build(tmp_path, "toolgate", "run.requested", {}) == ""


def test_planning_is_supervised_redispatch_then_escalate_then_retry(client, publisher) -> None:
    """A dispatched planner is a wait like any other: overdue with no draft
    -> one re-dispatch -> Owner escalation; `retry I1` re-dispatches planning."""
    import time as _time

    swarm = _start(client, publisher, PLAN_POLICY)
    overdue = _time.time() + PLAN_POLICY.dispatch_timeout_s + 1
    swarm.dispatcher.tick(swarm.state, overdue)
    swarm.pump()
    assert len(swarm.sent("plan.requested")) == 2       # the one re-dispatch

    swarm.dispatcher.tick(swarm.state, overdue + PLAN_POLICY.dispatch_timeout_s + 1)
    asks = [e for e in swarm.sent("decision.requested") if e.payload["subject_id"] == "I1"]
    assert len(asks) == 1                               # asked once, not twinned
    assert "planner" in asks[0].payload["reason"]

    publisher.send("owner", "interpreter", "decision.made",
                   {"gate_id": asks[0].payload["gate_id"], "subject_id": "I1",
                    "decision": "retry"})
    swarm.pump()
    assert len(swarm.sent("plan.requested")) == 3       # planning re-dispatched


def test_a_drafted_plan_moves_the_wait_to_the_owner(client, publisher) -> None:
    from relay.coordinator.diagnosis import waiting_on

    swarm = _start(client, publisher, PLAN_POLICY)
    items = {i.subject_id: i for i in waiting_on(swarm.state)}
    assert items["I1"].waiting_on == "planner"

    publisher.send("planner", "interpreter", "plan.drafted",
                   {"iteration_id": "I1", "summary": "extend the existing seam",
                    "plan_markdown": "# I1 plan\n...", "open_questions": ["extend or wrap?"]})
    swarm.pump()
    items = {i.subject_id: i for i in waiting_on(swarm.state)}
    assert items["I1"].waiting_on == "OWNER"
    assert "relay chat" in items["I1"].detail
