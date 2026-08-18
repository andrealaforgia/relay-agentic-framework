"""Unstoppable and re-entrant: waiting states supervise themselves, human
decisions can never evaporate, and 'what is stuck' is a fold, not a guess.

Regression suite for the ubi-es freeze: a decision.made arrived with the
wrong subject resolution and vanished silently, one behaviour stayed BLOCKED
for ever, and nothing told the Owner."""

from __future__ import annotations

import time

from test_coordinator import ROADMAP, SHA_BASE, MiniSwarm

from relay.coordinator.diagnosis import render, ts_epoch, waiting_on
from relay.coordinator.model import BehaviourState
from relay.coordinator.policy import Policy

SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"


def _start(client, publisher, policy=None) -> MiniSwarm:
    mini = MiniSwarm(client, publisher, policy=policy or Policy())
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    mini.pump()
    return mini


def _block_b1(swarm: MiniSwarm) -> str:
    """Escalate I1.S1.B1 to BLOCKED; returns the escalation's gate_id."""
    swarm.publisher.send(
        "coordinator", "interpreter", "decision.requested",
        {"gate_id": "gate-01M08YB2FF5X6KHTRJXR948MDV", "subject_id": "I1.S1.B1",
         "reason": "failed red-verification 3 times"},
    )
    swarm.pump()
    assert swarm.behaviour("I1.S1.B1").state == BehaviourState.BLOCKED
    return "gate-01M08YB2FF5X6KHTRJXR948MDV"


# ── the ubi-es bug, replayed exactly ─────────────────────────────────────────

def test_decision_without_subject_resolves_via_gate_id(client, publisher) -> None:
    swarm = _start(client, publisher)
    gate_id = _block_b1(swarm)
    # the EXACT shape that evaporated on ubi-es: gate_id, no subject_id
    publisher.send("interpreter", "coordinator", "decision.made",
                   {"gate_id": gate_id, "decision": "retry", "comment": "owner said so"})
    swarm.pump()
    b = swarm.behaviour("I1.S1.B1")
    assert b.state != BehaviourState.BLOCKED          # the way back worked
    assert swarm.state.decisions[gate_id].closed


def test_unmatched_decision_triggers_immediate_reask_with_exact_syntax(client, publisher) -> None:
    swarm = _start(client, publisher)
    _block_b1(swarm)
    publisher.send("interpreter", "coordinator", "decision.made",
                   {"gate_id": "gate-01M08YB2FF5X6KHTRJXR948AAA",  # unknown gate
                    "decision": "drop"})
    swarm.pump()
    asks = swarm.sent("decision.requested")
    assert len(asks) == 2                              # original + immediate re-ask
    assert "reply exactly" in asks[-1].payload["reason"]
    assert "drop I1.S1.B1" in asks[-1].payload["reason"]
    assert swarm.behaviour("I1.S1.B1").state == BehaviourState.BLOCKED  # never guessed


def test_duplicate_decision_is_idempotent_not_a_mismatch(client, publisher) -> None:
    swarm = _start(client, publisher)
    gate_id = _block_b1(swarm)
    for _ in range(2):
        publisher.send("interpreter", "coordinator", "decision.made",
                       {"gate_id": gate_id, "decision": "drop", "subject_id": "I1.S1.B1"})
    swarm.pump()
    assert swarm.behaviour("I1.S1.B1").state == BehaviourState.DONE
    assert len(swarm.sent("decision.requested")) == 1  # no spurious re-ask


def test_owner_published_decision_is_authoritative(client, publisher) -> None:
    swarm = _start(client, publisher)
    gate_id = _block_b1(swarm)
    # what the chat hook publishes from the Owner's literal `drop I1.S1.B1`
    publisher.send("owner", "interpreter", "decision.made",
                   {"gate_id": gate_id, "subject_id": "I1.S1.B1", "decision": "drop",
                    "comment": "drop I1.S1.B1"})
    swarm.pump()
    assert swarm.behaviour("I1.S1.B1").state == BehaviourState.DONE
    assert swarm.behaviour("I1.S1.B1").last_fail_reason == "dropped by the Owner"


# ── the clock: nudges and deadline supervision ───────────────────────────────

def _now_after(swarm: MiniSwarm, seconds: float) -> float:
    newest = max(ts_epoch(e.ts) for e in
                 (swarm.sent("decision.requested") + swarm.sent("spec.requested")
                  or [None]) if e)
    return newest + seconds


def test_open_decision_is_renudged_on_interval_forever(client, publisher) -> None:
    swarm = _start(client, publisher, Policy(decision_nudge_s=600))
    _block_b1(swarm)
    swarm.dispatcher.tick(swarm.state, _now_after(swarm, 30))
    assert len(swarm.sent("decision.requested")) == 1  # not yet due
    swarm.dispatcher.tick(swarm.state, _now_after(swarm, 700))
    swarm.pump()
    asks = swarm.sent("decision.requested")
    assert len(asks) == 2
    assert "reminder" in asks[-1].payload["reason"]
    # restarts never re-spam: a fresh fold carries last_ask forward
    fresh = MiniSwarm(client, publisher, policy=Policy(decision_nudge_s=600))
    fresh.pump()
    fresh.dispatcher.tick(fresh.state, ts_epoch(asks[-1].ts) + 30)
    assert len(fresh.sent("decision.requested")) == 2


def test_overdue_dispatch_is_redispatched_then_escalated(client, publisher) -> None:
    swarm = _start(client, publisher, Policy(dispatch_timeout_s=600))
    (first,) = swarm.sent("spec.requested")            # specifier never answers
    swarm.dispatcher.tick(swarm.state, ts_epoch(first.ts) + 700)
    swarm.pump()
    assert len(swarm.sent("spec.requested")) == 2      # re-dispatched once
    assert swarm.state.behaviours["I1.S1.B1"].same_state_dispatches == 1

    def b1_specs():
        return [e for e in swarm.sent("spec.requested")
                if e.payload.get("behaviour_id") == "I1.S1.B1"]

    second = b1_specs()[-1]
    swarm.dispatcher.tick(swarm.state, ts_epoch(second.ts) + 700)
    swarm.pump()
    assert len(b1_specs()) == 2                        # not a blind loop
    (ask,) = swarm.sent("decision.requested")          # escalated to the Owner
    assert "specifier" in ask.payload["reason"]
    assert swarm.behaviour("I1.S1.B1").state == BehaviourState.BLOCKED
    # and the line moved on: the block never froze the rest of the story
    assert any(e.payload.get("behaviour_id") != "I1.S1.B1"
               for e in swarm.sent("spec.requested"))


def test_gate_redispatch_supersedes_the_unanswered_gate(client, publisher) -> None:
    from test_gates import FULL_POLICY, _spec_and_build

    swarm = _start(client, publisher, FULL_POLICY)
    _spec_and_build(swarm, "I1.S1.B1")
    b = swarm.behaviour("I1.S1.B1")
    assert b.state == BehaviourState.GATES_PENDING
    gates_before = dict(b.pending_gates)
    latest = max(ts_epoch(g.since) for g in gates_before.values())
    swarm.dispatcher.tick(swarm.state, latest + 3600 + 100)
    swarm.pump()
    b = swarm.behaviour("I1.S1.B1")
    assert len(b.pending_gates) == len(gates_before)   # replaced, not accumulated
    assert set(b.pending_gates) != set(gates_before)   # fresh gate ids
    # answering the NEW gates passes them
    for g in list(b.pending_gates.values()):
        role = {"code_review": "reviewer", "test_design": "qa"}[g.gate]
        publisher.send(role, "coordinator", "gate.judged",
                       {"gate_id": g.gate_id, "verdict": "pass", "findings": []})
    swarm.pump()
    assert b.state in (BehaviourState.GATES_PASSED, BehaviourState.ACCEPTANCE_PENDING,
                       BehaviourState.SATISFIED_PENDING)


# ── the way back exists for EVERY escalation subject, not just behaviours ───

def _escalate_iteration_gate(swarm: MiniSwarm) -> str:
    gate_id = "gate-01M08YB2FF5X6KHTRJXR948MDW"
    swarm.publisher.send(
        "coordinator", "interpreter", "decision.requested",
        {"gate_id": gate_id, "subject_id": "I1",
         "reason": "iteration I1 gate failed: security"},
    )
    swarm.pump()
    assert swarm.state.iterations["I1"].escalated
    return gate_id


def test_iteration_escalation_drop_waives_the_gate(client, publisher) -> None:
    swarm = _start(client, publisher)
    gate_id = _escalate_iteration_gate(swarm)
    publisher.send("owner", "interpreter", "decision.made",
                   {"gate_id": gate_id, "subject_id": "I1", "decision": "drop",
                    "comment": "drop I1"})
    swarm.pump()
    it = swarm.state.iterations["I1"]
    assert not it.escalated and it.gates_waived and it.gates_passed()
    assert swarm.state.decisions[gate_id].closed
    assert not swarm.state.decision_mismatch


def test_iteration_escalation_retry_reruns_the_gate(client, publisher) -> None:
    swarm = _start(client, publisher)
    gate_id = _escalate_iteration_gate(swarm)
    publisher.send("owner", "interpreter", "decision.made",
                   {"gate_id": gate_id, "subject_id": "I1", "decision": "retry"})
    swarm.pump()
    it = swarm.state.iterations["I1"]
    assert not it.escalated and not it.gates_waived
    assert it.pending_gates == {}          # gates must be re-earned


def test_story_escalation_has_the_same_way_back(client, publisher) -> None:
    swarm = _start(client, publisher)
    gate_id = "gate-01M08YB2FF5X6KHTRJXR948MDX"
    publisher.send("coordinator", "interpreter", "decision.requested",
                   {"gate_id": gate_id, "subject_id": "I1.S1",
                    "reason": "story gate failed: mutation"})
    swarm.pump()
    assert swarm.state.stories["I1.S1"].escalated
    publisher.send("owner", "interpreter", "decision.made",
                   {"gate_id": gate_id, "subject_id": "I1.S1", "decision": "drop"})
    swarm.pump()
    story = swarm.state.stories["I1.S1"]
    assert not story.escalated and story.gates_waived and story.gates_passed()


def test_stale_nudge_after_the_answer_does_not_reescalate(client, publisher) -> None:
    """The ubi-es aftermath: retries applied on replay, then old-runtime
    nudges folded AFTER the answer re-flagged the iteration for ever."""
    swarm = _start(client, publisher)
    gate_id = _escalate_iteration_gate(swarm)
    publisher.send("owner", "interpreter", "decision.made",
                   {"gate_id": gate_id, "subject_id": "I1", "decision": "retry"})
    # a nudge that crossed the answer in flight (same gate_id, post-closure)
    publisher.send("coordinator", "interpreter", "decision.requested",
                   {"gate_id": gate_id, "subject_id": "I1",
                    "reason": "iteration I1 gate failed: security (reminder)"})
    swarm.pump()
    assert not swarm.state.iterations["I1"].escalated  # settled stays settled


def test_orphaned_escalation_reasks_itself(client, publisher) -> None:
    swarm = _start(client, publisher)
    gate_id = _escalate_iteration_gate(swarm)
    # simulate the lost-ask shape directly: escalated, but the ask is closed
    swarm.state.decisions[gate_id].closed = True
    asks_before = len(swarm.sent("decision.requested"))
    swarm.dispatcher.tick(swarm.state, time.time())
    swarm.pump()
    asks = swarm.sent("decision.requested")
    assert len(asks) == asks_before + 1
    assert "ask was lost" in asks[-1].payload["reason"]
    # and the fresh ask is answerable
    publisher.send("owner", "interpreter", "decision.made",
                   {"gate_id": asks[-1].payload["gate_id"], "subject_id": "I1",
                    "decision": "drop"})
    swarm.pump()
    assert swarm.state.iterations["I1"].gates_waived


# ── diagnosis: stuckness is a fold ───────────────────────────────────────────

def test_waiting_on_names_owner_and_roles(client, publisher) -> None:
    swarm = _start(client, publisher)
    gate_id = _block_b1(swarm)
    items = waiting_on(swarm.state)
    owner_items = [i for i in items if i.waiting_on == "OWNER"]
    assert any(i.subject_id == "I1.S1.B1" for i in owner_items)
    report = render(swarm.state, time.time())
    assert "OWNER" in report and "I1.S1.B1" in report

    publisher.send("owner", "interpreter", "decision.made",
                   {"gate_id": gate_id, "subject_id": "I1.S1.B1", "decision": "drop"})
    swarm.pump()
    assert all(i.subject_id != "I1.S1.B1" or i.waiting_on != "OWNER"
               for i in waiting_on(swarm.state))
