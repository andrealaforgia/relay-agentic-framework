"""A judge changing its mind is not a fix.

Replay of the ubi-es incident: the security gate failed I1 with findings,
`retry` re-ran it on the SAME commit, and the model passed it — nothing had
changed but the verdict. These tests make that shape unrepresentable: the
pass is contested, the findings ratchet holds, `fixed` claims against
unchanged code are rejected by arithmetic, and the Owner's `fix` turns
findings into actual rework."""

from __future__ import annotations

from test_coordinator import ROADMAP, MiniSwarm

from relay.coordinator.model import BehaviourState
from relay.coordinator.policy import GateSpec, Policy
from relay.coordinator.projection import findings_key

SHA_OLD = "a" * 40
SHA_NEW = "b" * 40
GATE_1 = "gate-01M08YB2FF5X6KHTRJXR948MD1"
GATE_2 = "gate-01M08YB2FF5X6KHTRJXR948MD2"
FINDING = {"severity": "major",
           "title": "No rate limiting on grant-token join/auth",
           "detail": "token join endpoint accepts unlimited guesses"}

SEC_POLICY = Policy(per_iteration=(GateSpec("security", "security"),))


def _start(client, publisher) -> MiniSwarm:
    mini = MiniSwarm(client, publisher, policy=SEC_POLICY)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    mini.pump()
    return mini


def _gate_round(swarm, gate_id, commit, verdict, findings=(), dispositions=None) -> None:
    swarm.publisher.send(
        "coordinator", "security", "gate.requested",
        {"gate_id": gate_id, "gate": "security", "subject_kind": "iteration",
         "subject_id": "I1", "commit_sha": commit, "base_sha": SHA_OLD},
        gate_id=gate_id,
    )
    payload: dict[str, object] = {"gate_id": gate_id, "verdict": verdict,
                                  "findings": list(findings)}
    if dispositions is not None:
        payload["dispositions"] = dispositions
    swarm.publisher.send("security", "coordinator", "gate.judged", payload)
    swarm.pump()


def _security_gate(swarm):
    return next(iter(swarm.state.iterations["I1"].pending_gates.values()))


def test_the_incident_a_flip_on_identical_code_is_contested(client, publisher) -> None:
    swarm = _start(client, publisher)
    _gate_round(swarm, GATE_1, SHA_OLD, "fail", findings=[FINDING])
    assert swarm.state.open_findings[findings_key("I1", "security")]

    _gate_round(swarm, GATE_2, SHA_OLD, "pass")     # the mind-change, verbatim
    gate = swarm.state.iterations["I1"].pending_gates[GATE_2]
    assert gate.verdict == "contested"
    assert "identical code" in gate.contested_reason
    assert not swarm.state.iterations["I1"].gates_passed()
    assert swarm.sent("iteration.finished") == []   # never marked done

    # the Owner sees it framed as what it is, with the way out
    asks = [a for a in swarm.sent("decision.requested")
            if a.payload["subject_id"] == "I1"]
    assert asks and "CONTESTED" in asks[-1].payload["reason"]
    assert "fix I1" in asks[-1].payload["reason"]
    # and the finding survived the flip
    assert swarm.state.open_findings[findings_key("I1", "security")]


def test_false_positive_claims_on_identical_code_are_still_contested(client, publisher) -> None:
    swarm = _start(client, publisher)
    _gate_round(swarm, GATE_1, SHA_OLD, "fail", findings=[FINDING])
    _gate_round(swarm, GATE_2, SHA_OLD, "pass", dispositions=[
        {"title": FINDING["title"], "disposition": "false_positive",
         "justification": "on reflection it seems fine"}])
    assert _has_contested(swarm)


def test_fixed_claim_citing_unchanged_code_is_rejected_by_arithmetic(client, publisher) -> None:
    swarm = _start(client, publisher)
    _gate_round(swarm, GATE_1, SHA_OLD, "fail", findings=[FINDING])
    _gate_round(swarm, GATE_2, SHA_NEW, "pass", dispositions=[
        {"title": FINDING["title"], "disposition": "fixed", "commit_sha": SHA_OLD}])
    gate = swarm.state.iterations["I1"].pending_gates[GATE_2]
    assert gate.verdict == "contested"
    assert "unchanged code" in gate.contested_reason


def test_undispositioned_findings_block_a_pass_even_on_new_code(client, publisher) -> None:
    swarm = _start(client, publisher)
    _gate_round(swarm, GATE_1, SHA_OLD, "fail", findings=[FINDING])
    _gate_round(swarm, GATE_2, SHA_NEW, "pass")     # new commit, silent amnesia
    gate = swarm.state.iterations["I1"].pending_gates[GATE_2]
    assert gate.verdict == "contested"
    assert "undispositioned" in gate.contested_reason


def test_an_honest_pass_on_changed_code_with_dispositions_is_honored(client, publisher) -> None:
    swarm = _start(client, publisher)
    _gate_round(swarm, GATE_1, SHA_OLD, "fail", findings=[FINDING])
    _gate_round(swarm, GATE_2, SHA_NEW, "pass", dispositions=[
        {"title": FINDING["title"], "disposition": "fixed", "commit_sha": SHA_NEW}])
    gate = swarm.state.iterations["I1"].pending_gates[GATE_2]
    assert gate.verdict == "pass"
    assert findings_key("I1", "security") not in swarm.state.open_findings


def test_fix_turns_findings_into_rework_on_the_integration_behaviour(client, publisher) -> None:
    swarm = _start(client, publisher)
    _gate_round(swarm, GATE_1, SHA_OLD, "fail", findings=[FINDING])
    _gate_round(swarm, GATE_2, SHA_OLD, "pass")     # contested -> escalated
    ask = [a for a in swarm.sent("decision.requested")
           if a.payload["subject_id"] == "I1"][-1]
    publisher.send("owner", "interpreter", "decision.made",
                   {"gate_id": ask.payload["gate_id"], "subject_id": "I1",
                    "decision": "fix", "comment": "fix I1"})
    swarm.pump()

    (rework,) = swarm.sent("rework.requested")
    assert rework.payload["behaviour_id"] == "I1.INT"
    titles = [f["title"] for f in rework.payload["findings"]]
    assert FINDING["title"] in titles
    assert swarm.state.behaviours["I1.INT"].state == BehaviourState.BUILD_DISPATCHED
    assert not swarm.state.iterations["I1"].fix_requested
    assert swarm.state.iterations["I1"].pending_gates == {}  # gates re-earned later

    # replay: a cold restart executes no second fix
    fresh = MiniSwarm(client, publisher, policy=SEC_POLICY)
    fresh.pump()
    assert len(fresh.sent("rework.requested")) == 1


def test_regate_after_rework_carries_the_prior_findings(client, publisher) -> None:
    from test_gates import FULL_POLICY, _spec_and_build

    swarm = MiniSwarm(client, publisher, policy=FULL_POLICY)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    swarm.pump()
    _spec_and_build(swarm, "I1.S1.B1")
    review = next(g for g in swarm.sent("gate.requested")
                  if g.payload["gate"] == "code_review")
    publisher.send("reviewer", "coordinator", "gate.judged",
                   {"gate_id": review.payload["gate_id"], "verdict": "fail",
                    "findings": [{"severity": "major", "title": "No timeout on API call",
                                  "detail": "hangs forever", "file": "src/api.py"}]})
    swarm.pump()  # -> rework dispatched
    b = swarm.behaviour("I1.S1.B1")
    assert b.state in (BehaviourState.BUILD_DISPATCHED, BehaviourState.SPEC_DISPATCHED)
    # builder returns with NEW code; gates re-request and must carry the memory
    publisher.send("builder", "coordinator", "behaviour.built",
                   {"behaviour_id": "I1.S1.B1", "story_id": "I1.S1", "iteration_id": "I1",
                    "commit_sha": SHA_NEW, "attempt": b.attempt})
    swarm.pump()
    at = swarm.sent("run.requested")[-1]
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": at.payload["run_id"], "kind": "acceptance_test",
                    "commit_sha": SHA_NEW, "exit_code": 0, "duration_s": 0.1,
                    "output_digest": "e" * 64})
    swarm.pump()
    regates = [g for g in swarm.sent("gate.requested")
               if g.payload["gate"] == "code_review"]
    assert len(regates) >= 2
    prior = regates[-1].payload.get("prior_findings")
    assert prior and prior[0]["title"] == "No timeout on API call"


def _has_contested(swarm) -> bool:
    return any(g.verdict == "contested"
               for g in swarm.state.iterations["I1"].pending_gates.values())
