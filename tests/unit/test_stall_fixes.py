"""The three dead-end fixes from the interpreter's stall report:
spec.satisfied (already-met criteria), the respec-loop cap, and
error.raised never vanishing."""

from __future__ import annotations

from test_coordinator import ROADMAP, SHA_SPEC, MiniSwarm

from relay.coordinator.model import BehaviourState


def _start(client, publisher) -> MiniSwarm:
    mini = MiniSwarm(client, publisher)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    mini.pump()
    return mini


def test_already_satisfied_criterion_completes_without_build(client, publisher) -> None:
    swarm = _start(client, publisher)
    publisher.send("specifier", "coordinator", "spec.satisfied",
                   {"behaviour_id": "I1.S1.B1", "test_paths": ["tests/guard.py"],
                    "commit_sha": SHA_SPEC,
                    "reason": "gravity from I1.S1.B0 already covers this"},
                   behaviour_id="I1.S1.B1")
    swarm.pump()
    check = swarm.sent("run.requested")[-1]  # the claim is machine-verified, not trusted
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": check.payload["run_id"], "kind": "acceptance_test",
                    "commit_sha": SHA_SPEC, "exit_code": 0, "duration_s": 0.2,
                    "output_digest": "d" * 64})
    swarm.pump()
    assert swarm.behaviour("I1.S1.B1").state == BehaviourState.DONE
    assert len(swarm.sent("build.requested")) == 0  # no build for existing behaviour


def test_false_satisfied_claim_goes_back_to_specifier(client, publisher) -> None:
    swarm = _start(client, publisher)
    publisher.send("specifier", "coordinator", "spec.satisfied",
                   {"behaviour_id": "I1.S1.B1", "test_paths": ["tests/guard.py"],
                    "commit_sha": SHA_SPEC, "reason": "already done (wrongly)"},
                   behaviour_id="I1.S1.B1")
    swarm.pump()
    check = swarm.sent("run.requested")[-1]
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": check.payload["run_id"], "kind": "acceptance_test",
                    "commit_sha": SHA_SPEC, "exit_code": 1, "duration_s": 0.2,
                    "output_digest": "d" * 64})
    swarm.pump()
    # not trusted: the failed guard sends the spec back, it does not complete
    assert swarm.behaviour("I1.S1.B1").state == BehaviourState.SPEC_DISPATCHED
    assert len(swarm.sent("spec.requested")) == 2


def test_respec_loop_escalates_instead_of_spinning_forever(client, publisher) -> None:
    swarm = _start(client, publisher)
    for round_ in range(2):  # two failed red-verifications (attempts 1 and 2)
        publisher.send("specifier", "coordinator", "spec.written",
                       {"behaviour_id": "I1.S1.B1", "test_paths": ["t.py"],
                        "commit_sha": SHA_SPEC, "touches": []})
        swarm.pump()
        red = swarm.sent("run.requested")[-1]
        publisher.send("toolgate", "coordinator", "run.completed",
                       {"run_id": red.payload["run_id"], "kind": "acceptance_test",
                        "commit_sha": SHA_SPEC, "exit_code": 0,  # 'failing' test passes
                        "duration_s": 0.1, "output_digest": "d" * 64})
        swarm.pump()
    # third failure hits max_attempts=3 spec dispatches -> blocked + owner decision
    publisher.send("specifier", "coordinator", "spec.written",
                   {"behaviour_id": "I1.S1.B1", "test_paths": ["t.py"],
                    "commit_sha": SHA_SPEC, "touches": []})
    swarm.pump()
    red = swarm.sent("run.requested")[-1]
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": red.payload["run_id"], "kind": "acceptance_test",
                    "commit_sha": SHA_SPEC, "exit_code": 0, "duration_s": 0.1,
                    "output_digest": "d" * 64})
    swarm.pump()
    assert swarm.behaviour("I1.S1.B1").state == BehaviourState.BLOCKED
    decisions = swarm.sent("decision.requested")
    assert len(decisions) == 1 and "red-verification" in decisions[0].payload["reason"]


def test_behaviour_error_escalates_and_blocks(client, publisher) -> None:
    swarm = _start(client, publisher)
    publisher.send("specifier", "coordinator", "error.raised",
                   {"behaviour_id": "I1.S1.B1", "kind": "blocked",
                    "detail": "cannot write a failing test for working behaviour"},
                   behaviour_id="I1.S1.B1")
    swarm.pump()
    assert swarm.behaviour("I1.S1.B1").state == BehaviourState.BLOCKED
    (decision,) = swarm.sent("decision.requested")
    assert "cannot write a failing test" in decision.payload["reason"]


def test_orphan_error_escalates_exactly_once_across_replay(client, publisher) -> None:
    swarm = _start(client, publisher)
    publisher.send("builder", "coordinator", "error.raised",
                   {"kind": "tool_failure", "detail": "git remote unreachable"})
    swarm.pump()
    decisions = swarm.sent("decision.requested")
    assert len(decisions) == 1
    assert decisions[0].payload["source_event_id"]

    fresh = MiniSwarm(client, publisher)  # cold restart, full replay
    fresh.pump()
    assert len(fresh.sent("decision.requested")) == 1  # not re-escalated
    assert fresh.state.unescalated_errors == {}


def test_iteration_finished_carries_how_to_try(client, publisher) -> None:
    from test_coordinator import SHA_BUILD, SHA_SPEC, _drive_behaviour_to_done

    swarm = _start(client, publisher)
    _drive_behaviour_to_done(swarm, "I1.S1.B1")
    _drive_behaviour_to_done(swarm, "I1.S1.INT")   # the story's own, first
    # then the iteration INT, with the builder providing run instructions
    publisher.send("specifier", "coordinator", "spec.written",
                   {"behaviour_id": "I1.INT", "test_paths": ["tests/int.py"],
                    "commit_sha": SHA_SPEC, "touches": []})
    swarm.pump()
    red = swarm.sent("run.requested")[-1]
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": red.payload["run_id"], "kind": "acceptance_test",
                    "commit_sha": SHA_SPEC, "exit_code": 1, "duration_s": 0.1,
                    "output_digest": "d" * 64})
    swarm.pump()
    publisher.send("builder", "coordinator", "behaviour.built",
                   {"behaviour_id": "I1.INT", "story_id": None, "iteration_id": "I1",
                    "commit_sha": SHA_BUILD, "attempt": 1,
                    "how_to_run": "uv run python -m rooms"},
                   behaviour_id="I1.INT")
    swarm.pump()
    green = swarm.sent("run.requested")[-1]
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": green.payload["run_id"], "kind": "acceptance_test",
                    "commit_sha": SHA_BUILD, "exit_code": 0, "duration_s": 0.1,
                    "output_digest": "e" * 64})
    swarm.pump()
    judgement = swarm.sent("judgement.requested")[-1]
    publisher.send("specifier", "coordinator", "acceptance.judged",
                   {"behaviour_id": "I1.INT", "verdict": "pass",
                    "run_id": judgement.payload["run_id"]})
    swarm.pump()
    (finished,) = swarm.sent("iteration.finished")
    assert finished.payload["how_to_try"] == "uv run python -m rooms"
