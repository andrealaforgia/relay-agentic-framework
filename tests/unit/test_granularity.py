"""The unit of transaction is not the unit of discipline.

$258.99 for two iterations, of which the specifier and builder were 45%,
almost all of it context re-acquisition: five cold contexts per behaviour,
each paying ~34k of harness floor and then rediscovering the codebase, for a
slice like "the piece moves right". Elephant Carpaccio assumes a slice is
free to take; here each one has a fixed setup cost.

At story granularity the specifier writes the story's failing tests in one
turn and the builder satisfies them in one session. What does NOT change: one
failing test per criterion, one commit per behaviour, one behaviour.built per
behaviour, gates on the diff, and the specifier still never satisfies its own
expectation.
"""

import pytest

from relay.coordinator.model import BehaviourState
from relay.coordinator.policy import GateSpec, Policy

from test_coordinator import ROADMAP, SHA_SPEC, MiniSwarm

STORY_MODE = Policy(
    per_behaviour=(GateSpec("code_review", "reviewer"), GateSpec("test_design", "qa")),
    spec_granularity="story", build_granularity="story",
)

TWO_BEHAVIOURS = {
    "iterations": [{
        "id": "I1", "goal": "See free rooms", "increment": "A CLI that lists free rooms",
        "stories": [{
            "id": "I1.S1", "title": "List free rooms",
            "narrative": "As a member, I want free rooms listed, so I can grab one.",
            "acceptance_criteria": [
                {"id": "I1.S1.B1", "text": "Only free rooms are listed."},
                {"id": "I1.S1.B2", "text": "A booked room is omitted."},
            ],
        }],
    }],
}


def _start(client, publisher, policy=STORY_MODE, roadmap=TWO_BEHAVIOURS) -> MiniSwarm:
    mini = MiniSwarm(client, publisher, policy=policy)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": roadmap, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    mini.pump()
    return mini


def test_the_specifier_is_handed_the_whole_story_at_once(client, publisher) -> None:
    swarm = _start(client, publisher)
    (spec,) = swarm.sent("spec.requested")
    criteria = [c["behaviour_id"] for c in spec.payload["criteria"]]
    assert criteria == ["I1.S1.B1", "I1.S1.B2", "I1.S1.INT"]
    # every one of them is now in flight, so nothing else is dispatched
    for bid in criteria:
        assert swarm.behaviour(bid).state == BehaviourState.SPEC_DISPATCHED
    assert len(swarm.sent("spec.requested")) == 1


def test_behaviour_granularity_still_asks_one_at_a_time(client, publisher) -> None:
    swarm = _start(client, publisher, policy=Policy())
    (spec,) = swarm.sent("spec.requested")
    assert "criteria" not in spec.payload
    assert spec.payload["behaviour_id"] == "I1.S1.B1"
    assert swarm.behaviour("I1.S1.B2").state == BehaviourState.PLANNED


def _go_red(swarm: MiniSwarm, bid: str) -> None:
    """One spec.written per behaviour, exactly as at behaviour granularity."""
    swarm.publisher.send("specifier", "coordinator", "spec.written",
                         {"behaviour_id": bid, "test_paths": [f"tests/{bid}.py"],
                          "commit_sha": SHA_SPEC, "touches": ["src/x.py"]})
    swarm.pump()
    red = [r for r in swarm.sent("run.requested")
           if r.payload["commit_sha"] == SHA_SPEC][-1]
    swarm.publisher.send("toolgate", "coordinator", "run.completed",
                         {"run_id": red.payload["run_id"], "kind": "acceptance_test",
                          "commit_sha": SHA_SPEC, "exit_code": 1, "duration_s": 0.1,
                          "output_digest": "d" * 64})
    swarm.pump()


def test_the_builder_waits_for_the_whole_story_to_go_red(client, publisher) -> None:
    swarm = _start(client, publisher)
    _go_red(swarm, "I1.S1.B1")
    assert swarm.sent("build.requested") == [], "half a story is not a story"

    _go_red(swarm, "I1.S1.B2")
    _go_red(swarm, "I1.S1.INT")
    (build,) = swarm.sent("build.requested")
    covered = [b["behaviour_id"] for b in build.payload["behaviours"]]
    assert covered == ["I1.S1.B1", "I1.S1.B2", "I1.S1.INT"]
    for bid in covered:
        assert swarm.behaviour(bid).state == BehaviourState.BUILD_DISPATCHED


def test_each_behaviour_is_still_delivered_and_gated_on_its_own(client, publisher) -> None:
    """The batching is the transaction, not the discipline."""
    swarm = _start(client, publisher)
    for bid in ("I1.S1.B1", "I1.S1.B2", "I1.S1.INT"):
        _go_red(swarm, bid)

    swarm.publisher.send("builder", "coordinator", "behaviour.built",
                         {"behaviour_id": "I1.S1.B1", "story_id": "I1.S1",
                          "iteration_id": "I1", "commit_sha": "c" * 40, "attempt": 1,
                          "summary": "only free rooms are listed"})
    swarm.pump()
    green = swarm.sent("run.requested")[-1]
    assert green.payload["commit_sha"] == "c" * 40      # its own run
    assert swarm.behaviour("I1.S1.B2").state == BehaviourState.BUILD_DISPATCHED


@pytest.mark.parametrize("field", ["spec_granularity", "build_granularity"])
def test_granularity_is_policy_not_code(field: str) -> None:
    assert getattr(Policy(), field) == "behaviour"       # unchanged unless asked
    assert getattr(STORY_MODE, field) == "story"


def test_a_batched_dispatch_survives_a_restart(client, publisher) -> None:
    """State is a fold over the ledger (D3). A story-wide request must move
    every behaviour it covers, or a cold start re-dispatches finished work —
    which is exactly what a replay of the scopa ledger did."""
    from relay.coordinator.model import SwarmState
    from relay.coordinator.projection import apply
    from relay.ledger.reader import read_all

    swarm = _start(client, publisher)
    assert len(swarm.sent("spec.requested")) == 1

    fresh = SwarmState()
    for _sid, env in read_all(client, "testswarm"):
        apply(fresh, env)
    for bid in ("I1.S1.B1", "I1.S1.B2", "I1.S1.INT"):
        assert fresh.behaviours[bid].state == BehaviourState.SPEC_DISPATCHED, bid


def test_a_batched_build_survives_a_restart(client, publisher) -> None:
    from relay.coordinator.model import SwarmState
    from relay.coordinator.projection import apply
    from relay.ledger.reader import read_all

    swarm = _start(client, publisher)
    for bid in ("I1.S1.B1", "I1.S1.B2", "I1.S1.INT"):
        _go_red(swarm, bid)
    assert len(swarm.sent("build.requested")) == 1

    fresh = SwarmState()
    for _sid, env in read_all(client, "testswarm"):
        apply(fresh, env)
    for bid in ("I1.S1.B1", "I1.S1.B2", "I1.S1.INT"):
        assert fresh.behaviours[bid].state == BehaviourState.BUILD_DISPATCHED, bid
