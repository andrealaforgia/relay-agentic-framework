"""An escalation must have a way back.

scopa, overnight: two behaviours blocked, both escalated to the Owner, and
nine hours of nothing. The Owner could not have cleared them even awake —
`decision.made` was consumed by nobody, so BLOCKED was a one-way door.

The second block was not a decision at all. The builder implemented "computer
plays automatically" and reported that two earlier acceptance tests now fail,
because they assert the table grows by exactly one card when the Owner plays.
That is an older expectation invalidated by a later behaviour. Only the
specifier may change tests, so it is rework — not a question for the Owner.
"""

from relay.coordinator.model import BehaviourState

from test_coordinator import ROADMAP, MiniSwarm


def _blocked(client, publisher, bid="I1.S1.B1") -> MiniSwarm:
    mini = MiniSwarm(client, publisher)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    mini.pump()
    mini.state.behaviours[bid].state = BehaviourState.BLOCKED
    return mini


def test_retry_puts_a_blocked_behaviour_back_to_work(client, publisher) -> None:
    swarm = _blocked(client, publisher)
    publisher.send("interpreter", "coordinator", "decision.made",
                   {"gate_id": "gate-01J5AB3CDEF4GH5JK6MN7PQ8RS", "decision": "retry",
                    "subject_id": "I1.S1.B1", "comment": "the test was wrong; try again"})
    swarm.pump()
    b = swarm.behaviour("I1.S1.B1")
    assert b.state != BehaviourState.BLOCKED
    assert b.attempt == 1, "a retry restores the attempt budget"
    assert swarm.sent("spec.requested"), "and the work is dispatched again"


def test_drop_takes_it_out_of_the_way(client, publisher) -> None:
    swarm = _blocked(client, publisher)
    publisher.send("interpreter", "coordinator", "decision.made",
                   {"gate_id": "gate-01J5AB3CDEF4GH5JK6MN7PQ8RS", "decision": "drop",
                    "subject_id": "I1.S1.B1"})
    swarm.pump()
    assert swarm.behaviour("I1.S1.B1").state == BehaviourState.DONE
    assert swarm.behaviour("I1.S1.B1").last_fail_reason == "dropped by the Owner"


def test_a_decision_about_something_else_changes_nothing(client, publisher) -> None:
    swarm = _blocked(client, publisher)
    publisher.send("interpreter", "coordinator", "decision.made",
                   {"gate_id": "gate-01J5AB3CDEF4GH5JK6MN7PQ8RS", "decision": "approve"})
    swarm.pump()
    assert swarm.behaviour("I1.S1.B1").state == BehaviourState.BLOCKED


def test_an_invalidated_test_is_the_specifiers_rework_not_the_owners_problem(
    client, publisher
) -> None:
    mini = MiniSwarm(client, publisher)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    mini.pump()
    publisher.send("builder", "coordinator", "error.raised",
                   {"behaviour_id": "I1.S1.B1", "kind": "spec_conflict",
                    "detail": "test_i1_s2_b1 asserts the table grows by exactly one card; "
                              "this behaviour makes the computer play too"},
                   behaviour_id="I1.S1.B1")
    mini.pump()

    rework = mini.sent("rework.requested")
    assert rework, "the specifier is asked to fix its own test"
    assert rework[-1].to_role == "specifier"
    assert "exactly one card" in rework[-1].payload["findings"][0]["detail"]
    assert mini.sent("decision.requested") == [], "the Owner is not asked"


def test_an_ordinary_error_still_reaches_the_owner(client, publisher) -> None:
    mini = MiniSwarm(client, publisher)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    mini.pump()
    publisher.send("builder", "coordinator", "error.raised",
                   {"behaviour_id": "I1.S1.B1", "kind": "push_conflict",
                    "detail": "branch diverged"}, behaviour_id="I1.S1.B1")
    mini.pump()
    assert mini.sent("decision.requested"), "a stuck assistant is still escalated"


# I4, prova-lang: the builder twice reported that an older acceptance fixture
# contradicted the new verifier. Both reports landed on behaviours already
# DONE, so `_error_raised` stored the conflict and `_advance_behaviours` —
# which skips terminal behaviours before `_advance_one` ever sees them — never
# routed it. The signal sat in state while the Owner was asked, for hours,
# about something else entirely. A conflict must stay actionable wherever it
# lands, and must act exactly once.


def _finished(client, publisher, bid, final) -> MiniSwarm:
    mini = MiniSwarm(client, publisher)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    mini.pump()
    mini.state.behaviours[bid].state = final
    return mini


def _raise_conflict(publisher, bid="I1.S1.B1") -> None:
    publisher.send("builder", "coordinator", "error.raised",
                   {"behaviour_id": bid, "kind": "spec_conflict",
                    "detail": "B23 E5 asserts a contract that is false at amount zero; "
                              "the verifier is right to refuse it"},
                   behaviour_id=bid)


def test_a_conflict_on_a_done_behaviour_is_still_routed(client, publisher) -> None:
    swarm = _finished(client, publisher, "I1.S1.B1", BehaviourState.DONE)
    _raise_conflict(publisher)
    swarm.pump()

    rework = swarm.sent("rework.requested")
    assert rework, "a conflict reported after the behaviour finished still reaches someone"
    assert rework[-1].to_role == "specifier"
    assert "false at amount zero" in rework[-1].payload["findings"][0]["detail"]
    assert swarm.behaviour("I1.S1.B1").state != BehaviourState.DONE


def test_a_conflict_on_a_blocked_behaviour_is_still_routed(client, publisher) -> None:
    swarm = _finished(client, publisher, "I1.S1.B1", BehaviourState.BLOCKED)
    _raise_conflict(publisher)
    swarm.pump()
    assert swarm.sent("rework.requested"), "BLOCKED is not a place conflicts go to die"


def test_a_conflict_on_a_finished_behaviour_acts_exactly_once(client, publisher) -> None:
    swarm = _finished(client, publisher, "I1.S1.B1", BehaviourState.DONE)
    _raise_conflict(publisher)
    swarm.pump()
    swarm.pump()
    assert len(swarm.sent("rework.requested")) == 1, "pumping again must not duplicate the work"

    fresh = MiniSwarm(client, publisher)
    fresh.pump()
    assert len(fresh.sent("rework.requested")) == 1, "nor may a cold restart replay it"
