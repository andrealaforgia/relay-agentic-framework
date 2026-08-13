"""Phase 2: gate machinery, characterization inversion, recon gating, PR flow."""

from __future__ import annotations

import json

from relay.coordinator.dispatcher import Dispatcher, GitHooks
from relay.coordinator.model import BehaviourState
from relay.coordinator.policy import GateSpec, Policy

from test_coordinator import ROADMAP, SHA_BASE, SHA_BUILD, SHA_SPEC, MiniSwarm

FULL_POLICY = Policy(
    per_behaviour=(GateSpec("code_review", "reviewer"), GateSpec("test_design", "qa")),
    per_story=(GateSpec("mutation", "qa"),),
    per_iteration=(GateSpec("security", "security"),),
)


def _start(client, publisher, policy=FULL_POLICY, roadmap=ROADMAP) -> MiniSwarm:
    mini = MiniSwarm(client, publisher, policy=policy)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": roadmap, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    mini.pump()
    return mini


def _spec_and_build(swarm: MiniSwarm, bid: str) -> None:
    p = swarm.publisher
    p.send("specifier", "coordinator", "spec.written",
           {"behaviour_id": bid, "test_paths": [f"tests/{bid}.py"],
            "commit_sha": SHA_SPEC, "touches": ["src/x.py"]})
    swarm.pump()
    red = swarm.sent("run.requested")[-1]
    exit_code = 0 if swarm.behaviour(bid).kind == "characterization" else 1
    p.send("toolgate", "coordinator", "run.completed",
           {"run_id": red.payload["run_id"], "kind": "acceptance_test", "commit_sha": SHA_SPEC,
            "exit_code": exit_code, "duration_s": 0.1, "output_digest": "d" * 64})
    swarm.pump()
    p.send("builder", "coordinator", "behaviour.built",
           {"behaviour_id": bid, "story_id": swarm.behaviour(bid).story_id,
            "iteration_id": "I1", "commit_sha": SHA_BUILD,
            "attempt": swarm.behaviour(bid).attempt})
    swarm.pump()
    at = swarm.sent("run.requested")[-1]
    p.send("toolgate", "coordinator", "run.completed",
           {"run_id": at.payload["run_id"], "kind": "acceptance_test", "commit_sha": SHA_BUILD,
            "exit_code": 0, "duration_s": 0.1, "output_digest": "e" * 64})
    swarm.pump()


def _pass_behaviour_gates(swarm: MiniSwarm, bid: str) -> None:
    gates = [g for g in swarm.sent("gate.requested")
             if g.payload["subject_id"] == bid and g.payload["subject_kind"] == "behaviour"
             and swarm.behaviour(bid).pending_gates.get(g.payload["gate_id"]) is not None]
    assert len(gates) == 2, f"expected reviewer+qa gates for {bid}"
    for g in gates:
        role = "reviewer" if g.payload["gate"] == "code_review" else "qa"
        swarm.publisher.send(role, "coordinator", "gate.judged",
                             {"gate_id": g.payload["gate_id"], "verdict": "pass", "findings": []})
    swarm.pump()


def _accept(swarm: MiniSwarm, bid: str) -> None:
    judgement = swarm.sent("judgement.requested")[-1]
    assert judgement.payload["behaviour_id"] == bid
    swarm.publisher.send("specifier", "coordinator", "acceptance.judged",
                         {"behaviour_id": bid, "verdict": "pass",
                          "run_id": judgement.payload["run_id"]})
    swarm.pump()


def _behaviour_to_done_gated(swarm: MiniSwarm, bid: str) -> None:
    _spec_and_build(swarm, bid)
    assert swarm.behaviour(bid).state == BehaviourState.GATES_PENDING
    _pass_behaviour_gates(swarm, bid)
    _accept(swarm, bid)
    assert swarm.behaviour(bid).state == BehaviourState.DONE


def test_fully_gated_iteration_with_mutation_security_and_pr(client, publisher) -> None:
    swarm = _start(client, publisher)
    _behaviour_to_done_gated(swarm, "I1.S1.B1")

    # story gate: mutation run requested by the coordinator, judged by qa
    mut_run = [r for r in swarm.sent("run.requested") if r.payload["kind"] == "mutation"]
    assert len(mut_run) == 1 and mut_run[0].story_id == "I1.S1"
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": mut_run[0].payload["run_id"], "kind": "mutation",
                    "commit_sha": SHA_BUILD, "exit_code": 1,  # survivors exist; qa judges
                    "duration_s": 5.0, "output_digest": "f" * 64})
    swarm.pump()
    mut_gate = [g for g in swarm.sent("gate.requested") if g.payload["gate"] == "mutation"]
    assert len(mut_gate) == 1
    assert mut_gate[0].payload["run_id"] == mut_run[0].payload["run_id"]
    publisher.send("qa", "coordinator", "gate.judged",
                   {"gate_id": mut_gate[0].payload["gate_id"], "verdict": "pass",
                    "findings": [], "score": 0.93})
    swarm.pump()
    assert len(swarm.sent("story.completed")) == 1

    # INT behaviour, then the iteration security gate
    _behaviour_to_done_gated(swarm, "I1.INT")
    sec_gate = [g for g in swarm.sent("gate.requested") if g.payload["gate"] == "security"]
    assert len(sec_gate) == 1 and sec_gate[0].payload["subject_kind"] == "iteration"
    publisher.send("security", "coordinator", "gate.judged",
                   {"gate_id": sec_gate[0].payload["gate_id"], "verdict": "pass", "findings": []})
    swarm.pump()
    assert len(swarm.sent("iteration.finished")) == 1

    # owner approves the PR at the checkpoint
    publisher.send("interpreter", "coordinator", "pr.approved",
                   {"iteration_id": "I1", "gate_id": "gate-01J5AB3CDEF4GH5JK6MN7PQ8RS"})
    swarm.pump()
    (pr,) = swarm.sent("pr.opened")
    assert pr.payload["pr_url"].startswith("https://")


def test_gate_failure_causes_rework_with_findings(client, publisher) -> None:
    swarm = _start(client, publisher)
    _spec_and_build(swarm, "I1.S1.B1")
    gates = [g for g in swarm.sent("gate.requested")]
    review = next(g for g in gates if g.payload["gate"] == "code_review")
    qa = next(g for g in gates if g.payload["gate"] == "test_design")
    publisher.send("qa", "coordinator", "gate.judged",
                   {"gate_id": qa.payload["gate_id"], "verdict": "pass", "findings": []})
    publisher.send("reviewer", "coordinator", "gate.judged",
                   {"gate_id": review.payload["gate_id"], "verdict": "fail",
                    "findings": [{"severity": "major", "title": "No timeout on API call",
                                  "detail": "calendar client call can hang forever"}]})
    swarm.pump()
    rework = swarm.sent("rework.requested")
    assert len(rework) == 1
    assert rework[0].payload["attempt"] == 2
    assert swarm.behaviour("I1.S1.B1").state == BehaviourState.BUILD_DISPATCHED
    assert len(swarm.sent("judgement.requested")) == 0  # no judgement past a failed gate


def test_mutation_gate_failure_reworks_and_reruns_mutation(client, publisher) -> None:
    swarm = _start(client, publisher)
    _behaviour_to_done_gated(swarm, "I1.S1.B1")
    mut_run = [r for r in swarm.sent("run.requested") if r.payload["kind"] == "mutation"][-1]
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": mut_run.payload["run_id"], "kind": "mutation",
                    "commit_sha": SHA_BUILD, "exit_code": 1, "duration_s": 5.0,
                    "output_digest": "f" * 64})
    swarm.pump()
    mut_gate = [g for g in swarm.sent("gate.requested") if g.payload["gate"] == "mutation"][-1]
    publisher.send("qa", "coordinator", "gate.judged",
                   {"gate_id": mut_gate.payload["gate_id"], "verdict": "fail",
                    "findings": [{"severity": "major", "title": "Unjustified survivor",
                                  "detail": "boundary mutant in free() survives"}],
                    "score": 0.72})
    swarm.pump()
    # the story loops back through the builder; gates must be re-earned
    assert swarm.behaviour("I1.S1.B1").state == BehaviourState.BUILD_DISPATCHED
    assert swarm.state.stories["I1.S1"].mutation_run_id is None
    assert len(swarm.sent("story.completed")) == 0


def test_characterization_red_verification_is_inverted(client, publisher) -> None:
    roadmap = json.loads(json.dumps(ROADMAP))
    roadmap["iterations"][0]["stories"][0]["acceptance_criteria"].insert(0, {
        "id": "I1.S1.CHAR1",
        "text": "Current booking lookup behaviour is pinned before changes.",
    })
    swarm = _start(client, publisher, roadmap=roadmap)
    assert swarm.behaviour("I1.S1.CHAR1").kind == "characterization"
    publisher.send("specifier", "coordinator", "spec.written",
                   {"behaviour_id": "I1.S1.CHAR1", "test_paths": ["tests/char.py"],
                    "commit_sha": SHA_SPEC, "touches": []})
    swarm.pump()
    red = swarm.sent("run.requested")[-1]
    # a characterization test that FAILS against current behaviour is wrong
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": red.payload["run_id"], "kind": "acceptance_test",
                    "commit_sha": SHA_SPEC, "exit_code": 1, "duration_s": 0.1,
                    "output_digest": "d" * 64})
    swarm.pump()
    assert swarm.behaviour("I1.S1.CHAR1").state == BehaviourState.SPEC_DISPATCHED  # re-spec


def test_recon_runs_first_and_risk_areas_block_uncharacterized_builds(client, publisher) -> None:
    mini = MiniSwarm(client, publisher, policy=Policy(),
                     git=GitHooks(ensure_branch=lambda _i: SHA_BASE, head_sha=lambda: SHA_BASE,
                                  has_history=lambda: True,
                                  create_pr=lambda _i: "https://x/pr/1"))
    mini.pump()
    assert len(mini.sent("recon.requested")) == 1  # recon before any roadmap
    publisher.send("analyst", "interpreter", "recon.completed",
                   {"brief_path": "docs/codebase-brief.md",
                    "risk_areas": ["src/x.py"], "coverage_summary": "untested"})
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "legacy"}})
    publisher.send("interpreter", "coordinator", "iteration.started",
                   {"iteration_id": "I1"})
    mini.pump()
    # behaviour touches src/x.py (a risk area) and its story has no CHAR behaviour
    publisher.send("specifier", "coordinator", "spec.written",
                   {"behaviour_id": "I1.S1.B1", "test_paths": ["tests/t.py"],
                    "commit_sha": SHA_SPEC, "touches": ["src/x.py"]})
    mini.pump()
    red = mini.sent("run.requested")[-1]
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": red.payload["run_id"], "kind": "acceptance_test",
                    "commit_sha": SHA_SPEC, "exit_code": 1, "duration_s": 0.1,
                    "output_digest": "d" * 64})
    mini.pump()
    assert mini.behaviour("I1.S1.B1").state == BehaviourState.BLOCKED
    decision = mini.sent("decision.requested")[-1]
    assert "characterization" in decision.payload["reason"]
    assert len(mini.sent("build.requested")) == 0  # fail closed
