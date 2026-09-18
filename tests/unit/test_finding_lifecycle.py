"""Findings are settled one at a time, not a gate at a time.

Replay of the I3 security stall. The security gate failed I3 with six
findings. A later run confirmed three of them fixed, by commits that really
had changed the code, but still failed because of one remaining finding. The
fold only read dispositions from a PASSING verdict, so the three repaired
findings stayed open, and the Owner's `fix` sent all six back to a builder:
three already fixed, one design risk the Owner had accepted in the approved
plan, and minor observations the gate's own rules say never fail it.

What must hold instead:
- a verified `fixed` disposition settles its finding even when another
  finding keeps the verdict failing; the finding is kept, marked, not erased;
- minor and nit findings are observations: recorded, never dispatched as work
  and never demanded back as prior findings;
- a risk the Owner accepts, by title and on the record, stops being work and
  no longer fails the gate on its own, while anything new still fails it;
- rework carries only what is still unresolved and blocking.
"""

from __future__ import annotations

import json
from pathlib import Path

from test_coordinator import ROADMAP, SHA_BUILD, MiniSwarm, _drive_behaviour_to_done

from relay.coordinator.model import BehaviourState
from relay.coordinator.policy import GateSpec, Policy
from relay.coordinator.projection import findings_key

SEC_POLICY = Policy(per_iteration=(GateSpec("security", "security"),))
SHA_REWORKED = "d" * 40


def _preserved() -> dict[int, dict[str, object]]:
    path = Path(__file__).parent.parent / "fixtures" / "i3_stall_events.json"
    events = {}
    for e in json.loads(path.read_text()):
        payload = e["payload"]
        e["payload"] = json.loads(payload) if isinstance(payload, str) else payload
        events[int(e["seq"])] = e
    return events


PRESERVED = _preserved()
SIX = PRESERVED[13720]["payload"]["findings"]            # what `fix` dispatched
PARTIAL = PRESERVED[13612]["payload"]                    # fail, three fixed
BLOCKER, MAJOR = SIX[0]["title"], SIX[1]["title"]
MINORS = {f["title"] for f in SIX if f.get("severity") == "minor"}
FIXED = {d["title"] for d in PARTIAL["dispositions"] if d["disposition"] == "fixed"}
D3 = ("Accepted in the approved I3 plan (D3): write-time confinement is "
      "deferred; the residual race is disclosed in ConfineOutput's own comment.")


def _to_security_gate(client, publisher) -> MiniSwarm:
    swarm = MiniSwarm(client, publisher, policy=SEC_POLICY)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    swarm.pump()
    for bid in ("I1.S1.B1", "I1.S1.INT", "I1.INT"):
        _drive_behaviour_to_done(swarm, bid)
    assert swarm.sent("gate.requested"), "the iteration never reached its security gate"
    return swarm


def _judge(swarm: MiniSwarm, verdict: str, findings, dispositions=None) -> None:
    gate = swarm.sent("gate.requested")[-1]
    payload: dict[str, object] = {"gate_id": gate.payload["gate_id"],
                                  "verdict": verdict, "findings": list(findings)}
    if dispositions is not None:
        payload["dispositions"] = dispositions
    swarm.publisher.send("security", "coordinator", "gate.judged", payload,
                         in_reply_to=gate.event_id)
    swarm.pump()


def _answer(swarm: MiniSwarm, subject: str, decision: str, **extra: object) -> None:
    ask = [a for a in swarm.sent("decision.requested")
           if a.payload["subject_id"] == subject][-1]
    swarm.publisher.send("interpreter", "coordinator", "decision.made",
                         {"gate_id": ask.payload["gate_id"], "decision": decision, **extra})
    swarm.pump()


def _rebuild(swarm: MiniSwarm, sha: str) -> None:
    """The builder answers the latest rework, and the rebuilt increment passes
    its own checks again, so the gate re-runs on changed code."""
    p = swarm.publisher
    rework = swarm.sent("rework.requested")[-1]
    p.send("builder", "coordinator", "behaviour.built",
           {"behaviour_id": "I1.INT", "story_id": None, "iteration_id": "I1",
            "commit_sha": sha, "attempt": rework.payload["attempt"]},
           in_reply_to=rework.event_id)
    swarm.pump()
    run = swarm.sent("run.requested")[-1]
    p.send("toolgate", "coordinator", "run.completed",
           {"run_id": run.payload["run_id"], "kind": "acceptance_test", "commit_sha": sha,
            "exit_code": 0, "duration_s": 1.0, "output_digest": "e" * 64})
    swarm.pump()
    judgement = swarm.sent("judgement.requested")[-1]
    p.send("specifier", "coordinator", "acceptance.judged",
           {"behaviour_id": "I1.INT", "verdict": "pass",
            "run_id": judgement.payload["run_id"]})
    swarm.pump()


def _titles(findings) -> set[str]:
    return {str(f["title"]) for f in findings}


def _after_partial_fix(client, publisher) -> MiniSwarm:
    """Six found; `fix`; rebuilt; the re-run confirms three fixed but fails."""
    swarm = _to_security_gate(client, publisher)
    _judge(swarm, "fail", SIX)
    _answer(swarm, "I1", "fix", comment="fix I1")
    _rebuild(swarm, SHA_REWORKED)
    _judge(swarm, "fail", PARTIAL["findings"], PARTIAL["dispositions"])
    return swarm


# ── what `fix` hands the builder ──────────────────────────────────────────────

def test_fix_dispatches_only_blocking_findings(client, publisher) -> None:
    swarm = _to_security_gate(client, publisher)
    _judge(swarm, "fail", SIX)
    _answer(swarm, "I1", "fix", comment="fix I1")
    (rework,) = swarm.sent("rework.requested")
    assert _titles(rework.payload["findings"]) == {BLOCKER, MAJOR}


def test_minor_findings_are_kept_as_observations(client, publisher) -> None:
    swarm = _to_security_gate(client, publisher)
    _judge(swarm, "fail", SIX)
    key = findings_key("I1", "security")
    assert _titles(swarm.state.open_findings[key]) == {BLOCKER, MAJOR}
    observed = {f["title"] for f in swarm.state.finding_history[key]
                if f["status"] == "observation"}
    assert observed == MINORS


def test_the_regate_demands_dispositions_only_for_blocking_findings(client, publisher) -> None:
    swarm = _to_security_gate(client, publisher)
    _judge(swarm, "fail", SIX)
    _answer(swarm, "I1", "fix", comment="fix I1")
    _rebuild(swarm, SHA_REWORKED)
    regate = swarm.sent("gate.requested")[-1]
    assert _titles(regate.payload["prior_findings"]) == {BLOCKER, MAJOR}


# ── the partial-disposition failure, replayed ────────────────────────────────

def test_verified_fixes_settle_even_when_the_verdict_still_fails(client, publisher) -> None:
    swarm = _after_partial_fix(client, publisher)
    key = findings_key("I1", "security")
    assert _titles(swarm.state.open_findings[key]) == {MAJOR}
    fixed = {f["title"]: f for f in swarm.state.finding_history[key]
             if f["status"] == "fixed"}
    assert BLOCKER in fixed
    by_title = {d["title"]: d for d in PARTIAL["dispositions"]}
    assert fixed[BLOCKER]["fixed_by"] == by_title[BLOCKER]["commit_sha"]


def test_a_second_fix_never_resends_repaired_findings(client, publisher) -> None:
    swarm = _after_partial_fix(client, publisher)
    _answer(swarm, "I1", "fix", comment="fix I1")
    latest = swarm.sent("rework.requested")[-1]
    assert _titles(latest.payload["findings"]) == {MAJOR}
    assert not (_titles(latest.payload["findings"]) & FIXED)


def test_a_fixed_claim_on_unchanged_code_settles_nothing(client, publisher) -> None:
    swarm = _to_security_gate(client, publisher)
    _judge(swarm, "fail", SIX)
    _answer(swarm, "I1", "fix", comment="fix I1")
    _rebuild(swarm, SHA_REWORKED)
    # cites the very commit the blocker was found on
    _judge(swarm, "fail", [SIX[1]], [
        {"title": BLOCKER, "disposition": "fixed", "commit_sha": SHA_BUILD}])
    key = findings_key("I1", "security")
    assert BLOCKER in _titles(swarm.state.open_findings[key])


# ── the Owner's acceptance of one named risk ─────────────────────────────────

def _accept_d3(swarm: MiniSwarm) -> None:
    _answer(swarm, "I1", "retry", comment="Keep D3 as approved.",
            accepted_risks=[{"subject_id": "I1", "title": MAJOR, "justification": D3}])


def test_an_accepted_risk_is_no_longer_work(client, publisher) -> None:
    swarm = _after_partial_fix(client, publisher)
    reworks_before = len(swarm.sent("rework.requested"))
    _accept_d3(swarm)
    assert len(swarm.sent("rework.requested")) == reworks_before
    key = findings_key("I1", "security")
    assert not swarm.state.open_findings.get(key)
    accepted = [f for f in swarm.state.finding_history[key] if f["status"] == "risk_accepted"]
    assert _titles(accepted) == {MAJOR}
    assert accepted[0]["justification"] == D3


def test_the_regate_is_told_which_risks_the_owner_accepted(client, publisher) -> None:
    swarm = _after_partial_fix(client, publisher)
    _accept_d3(swarm)
    regate = swarm.sent("gate.requested")[-1]
    assert _titles(regate.payload["accepted_risks"]) == {MAJOR}
    assert "prior_findings" not in regate.payload


def test_a_fail_made_only_of_the_accepted_risk_and_observations_is_honoured(
        client, publisher) -> None:
    swarm = _after_partial_fix(client, publisher)
    _accept_d3(swarm)
    _judge(swarm, "fail", PARTIAL["findings"])        # the same three, again
    gate = list(swarm.state.iterations["I1"].pending_gates.values())[-1]
    assert gate.verdict == "pass"
    assert gate.accepted == [MAJOR]
    assert len(swarm.sent("iteration.finished")) == 1


def test_an_accepted_risk_never_excuses_a_new_blocking_finding(client, publisher) -> None:
    swarm = _after_partial_fix(client, publisher)
    _accept_d3(swarm)
    fresh = {"severity": "major", "title": "Output path traversal via ..",
             "detail": "a relative destination escapes the project root"}
    _judge(swarm, "fail", [*PARTIAL["findings"], fresh])
    gate = list(swarm.state.iterations["I1"].pending_gates.values())[-1]
    assert gate.verdict == "fail"
    assert swarm.sent("iteration.finished") == []
    assert swarm.state.iterations["I1"].int_behaviour_id
    assert swarm.behaviour("I1.INT").state == BehaviourState.DONE


def test_a_verified_fix_of_an_observation_is_recorded_as_a_fix(client, publisher) -> None:
    """Two of the three findings the re-run confirmed fixed were minor: the
    record must say they were fixed, and by what, not merely observed."""
    swarm = _after_partial_fix(client, publisher)
    key = findings_key("I1", "security")
    status = {f["title"]: f for f in swarm.state.finding_history[key]}
    by_title = {d["title"]: d for d in PARTIAL["dispositions"]}
    for title in FIXED:
        assert status[title]["status"] == "fixed", title
        assert status[title]["fixed_by"] == by_title[title]["commit_sha"]
    assert {t for t, f in status.items() if f["status"] == "observation"} == MINORS - FIXED
