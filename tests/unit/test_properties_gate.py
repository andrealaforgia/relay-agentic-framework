"""The property suite as a deterministic gate: invariants get enforced, a
failure becomes rework with the counterexample, and nothing flips on a seed
because gates run derandomized (a policy of the playbooks, mechanics here)."""

from __future__ import annotations

from test_coordinator import ROADMAP, MiniSwarm
from test_gates import _spec_and_build

from relay.coordinator.model import BehaviourState
from relay.coordinator.policy import Policy

SHA = "c" * 40


def _start(client, publisher, scope: str) -> MiniSwarm:
    mini = MiniSwarm(client, publisher, policy=Policy(properties=scope))
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    mini.pump()
    return mini


def _complete_story_behaviours(swarm: MiniSwarm) -> None:
    for bid in list(swarm.state.stories["I1.S1"].behaviour_ids):
        _spec_and_build(swarm, bid)
        judgement = swarm.sent("judgement.requested")[-1]
        swarm.publisher.send("specifier", "coordinator", "acceptance.judged",
                             {"behaviour_id": bid, "verdict": "pass",
                              "run_id": judgement.payload["run_id"]})
        swarm.pump()


def _properties_run(swarm: MiniSwarm):
    runs = [r for r in swarm.sent("run.requested") if r.payload["kind"] == "properties"]
    return runs[-1] if runs else None


def test_story_scope_runs_suite_before_story_completed(client, publisher) -> None:
    swarm = _start(client, publisher, "story")
    _complete_story_behaviours(swarm)
    run = _properties_run(swarm)
    assert run is not None and run.story_id == "I1.S1"
    assert swarm.sent("story.completed") == []          # gated until the run lands

    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": run.payload["run_id"], "kind": "properties",
                    "commit_sha": run.payload["commit_sha"], "exit_code": 0,
                    "duration_s": 2.0, "output_digest": "d" * 64})
    swarm.pump()
    assert len(swarm.sent("story.completed")) == 1      # green suite releases it


def test_failed_suite_becomes_rework_with_the_counterexample(client, publisher) -> None:
    swarm = _start(client, publisher, "story")
    _complete_story_behaviours(swarm)
    run = _properties_run(swarm)
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": run.payload["run_id"], "kind": "properties",
                    "commit_sha": run.payload["commit_sha"], "exit_code": 1,
                    "duration_s": 2.0, "output_digest": "d" * 64,
                    "summary": "Falsifying example: total=Decimal('0.015') rounds before summing"})
    swarm.pump()
    assert swarm.sent("story.completed") == []
    rework = swarm.sent("rework.requested")[-1]
    assert rework.payload["behaviour_id"] == "I1.S1.INT"
    detail = rework.payload["findings"][0]["detail"]
    assert "Falsifying example" in detail and run.payload["run_id"] in detail
    assert swarm.state.behaviours["I1.S1.INT"].state == BehaviourState.BUILD_DISPATCHED
    # the story must re-earn the suite after the fix (run id cleared by the fold)
    assert swarm.state.stories["I1.S1"].properties_run_id is None

    # replay: a fresh coordinator re-dispatches nothing extra
    fresh = MiniSwarm(client, publisher, policy=Policy(properties="story"))
    fresh.pump()
    assert len(fresh.sent("rework.requested")) == len(swarm.sent("rework.requested"))


def test_off_scope_changes_nothing(client, publisher) -> None:
    swarm = _start(client, publisher, "off")
    _complete_story_behaviours(swarm)
    assert _properties_run(swarm) is None
    assert len(swarm.sent("story.completed")) == 1
