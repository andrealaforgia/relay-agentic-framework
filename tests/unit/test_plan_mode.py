"""Plan mode: no behaviour is dispatched until an Owner-approved change plan
is committed — a dispatcher rule, nudged to the Owner exactly once, unblocked
by plan.committed, exact across restarts."""

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


def test_plan_gate_blocks_dispatch_and_nudges_once(client, publisher) -> None:
    swarm = _start(client, publisher, PLAN_POLICY)
    assert swarm.sent("spec.requested") == []           # nothing dispatched
    (nudge,) = swarm.sent("stall.detected")             # the Owner is told once
    assert nudge.payload["waiting_on"] == "planner"
    assert nudge.payload["subject_id"] == "I1"
    swarm.pump()
    assert len(swarm.sent("stall.detected")) == 1       # never re-nagged

    # cold restart: replay produces no second nudge and still no dispatch
    fresh = MiniSwarm(client, publisher, policy=PLAN_POLICY)
    fresh.pump()
    assert len(fresh.sent("stall.detected")) == 1
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
