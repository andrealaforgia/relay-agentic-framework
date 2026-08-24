"""Replay of the ubies double-ask incident (2026-08-24).

Every escalation on that swarm arrived twice in the same second: the real ask
plus an 'ask was lost' twin (ledger seq 92/93, 109/110, 132/133). The live
coordinator publishes an escalation in react() and runs tick() microseconds
later — before the ask's echo has folded back from the ledger — so the orphan
re-asker saw BLOCKED-with-no-decision and asked again. Then answering one
twin unblocked the subject, which made the other twin's answer 'match
nothing' (seq 157-160), leaving it open and re-nudging forever.
"""

from __future__ import annotations

import time

from test_coordinator import MiniSwarm, swarm  # noqa: F401  (fixture)

from relay.coordinator.model import BehaviourState

G1 = "gate-01M0TKDTCZ37R968G150QZHQP8"
G2 = "gate-01M0TKDTD0EFJ478HT72YSHQSG"


def _open_twins(swarm: MiniSwarm) -> None:
    """Two open decisions for one blocked behaviour — the incident's shape."""
    for gate_id, reason in (
        (G1, "behaviour I1.S1.B1 blocked after 3 attempts: AT still failing"),
        (G2, "I1.S1.B1 is escalated but its ask was lost — reply exactly `retry I1.S1.B1`"),
    ):
        swarm.publisher.send("coordinator", "interpreter", "decision.requested",
                             {"gate_id": gate_id, "subject_id": "I1.S1.B1",
                              "reason": reason})
    swarm.pump()
    swarm.state.behaviours["I1.S1.B1"].state = BehaviourState.BLOCKED


def test_an_escalation_is_not_doubled_by_its_own_tick(swarm: MiniSwarm) -> None:
    """react() escalates; tick() fires before the ask's echo folds back.
    The in-memory mirror is what stops the 'ask was lost' twin."""
    b = swarm.state.behaviours["I1.S1.B1"]
    b.attempt = 3  # at the limit: the next failure blocks
    swarm.dispatcher._rework_or_escalate(swarm.state, b,
                                         "acceptance test still failing after build")
    swarm.dispatcher.tick(swarm.state, time.time())

    asks = [e for e in swarm.sent("decision.requested")
            if e.payload.get("subject_id") == "I1.S1.B1"]
    assert len(asks) == 1
    assert "ask was lost" not in str(asks[0].payload.get("reason"))


def test_one_answer_settles_every_twin_ask_for_the_subject(swarm: MiniSwarm) -> None:
    _open_twins(swarm)
    assert sum(1 for d in swarm.state.decisions.values() if not d.closed) == 2

    swarm.publisher.send("owner", "interpreter", "decision.made",
                         {"gate_id": G1, "subject_id": "I1.S1.B1", "decision": "retry"})
    swarm.pump()

    assert all(d.closed for d in swarm.state.decisions.values())
    assert swarm.state.behaviours["I1.S1.B1"].state != BehaviourState.BLOCKED
    assert not swarm.state.decision_mismatch


def test_a_late_answer_to_a_settled_twin_is_absorbed_not_mismatched(swarm: MiniSwarm) -> None:
    """seq 156: `fix` on the twin after its sibling already unblocked the
    subject. That must close quietly — 'your last decision matched nothing'
    on a valid answer is how the Owner ends up in a nudge loop."""
    _open_twins(swarm)
    swarm.publisher.send("owner", "interpreter", "decision.made",
                         {"gate_id": G1, "subject_id": "I1.S1.B1", "decision": "retry"})
    swarm.pump()
    # the twin is already closed by absorption; answer it again anyway
    swarm.state.decisions[G2].closed = False   # a replayed old runtime's view
    swarm.publisher.send("owner", "interpreter", "decision.made",
                         {"gate_id": G2, "subject_id": "I1.S1.B1", "decision": "fix"})
    swarm.pump()

    assert swarm.state.decisions[G2].closed
    assert not swarm.state.decision_mismatch
