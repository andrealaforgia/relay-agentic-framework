"""A story that desynchronizes must still converge.

friend-positions, an afternoon of nothing. A Claude usage limit wedged every
worker at once: each turn came back with no reply in three seconds, relay
counted three attempts and quarantined seventeen messages into the DLQ as
`unparseable`. The coordinator escalated I2.S5's four behaviours to the Owner,
who answered `retry` to each of them in turn.

That order is the bug. `retry I2.S5.B1` landed first, so B1 alone was
re-planned and dispatched — its siblings were still BLOCKED, so the spec batch
held only B1. B1 went red, correctly, and stopped there. Fifteen seconds later
the siblings were re-planned too, and were never dispatched again:

  * wip_limit is 1, and _advance_behaviours counted BEHAVIOURS in flight, so
    red-verified B1 filled the whole budget;
  * build_granularity is `story`, so _build_batch made B1 wait for every
    sibling to go red before any build could go out.

B1 waited for its siblings; the siblings waited for the slot B1 held. Nothing
was dispatched, so no deadline covered it, and `return 0` said nothing — the
coordinator's own move going unmade is still the one wait nothing supervises.
The board read `red_verified` for ninety minutes and two `relay down &&
relay up` cycles rebuilt the same deadlock from the same ledger.
"""

from __future__ import annotations

from relay.coordinator.model import BehaviourState
from relay.coordinator.policy import Policy

from test_coordinator import SHA_SPEC, MiniSwarm
from test_granularity import STORY_MODE, TWO_BEHAVIOURS, _go_red

STORY = "I1.S1"
B1, B2, INT = "I1.S1.B1", "I1.S1.B2", "I1.S1.INT"
ITER_INT = "I1.INT"                            # a unit of its own: no story_id
GATES = "gate-01M08YB2FF5X6KHTRJXR948MD"       # + one char per subject


def _epoch(iso: str) -> float:
    from relay.coordinator.diagnosis import ts_epoch

    return ts_epoch(iso)


def _start(client, publisher, policy: Policy = STORY_MODE) -> MiniSwarm:
    mini = MiniSwarm(client, publisher, policy=policy)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": TWO_BEHAVIOURS, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started",
                   {"iteration_id": "I1"})
    mini.pump()
    return mini


def _escalate(swarm: MiniSwarm, bid: str, suffix: str) -> str:
    """Wedge one behaviour the way the usage limit did: an open escalation,
    which the fold turns into BLOCKED."""
    gate_id = GATES + suffix
    swarm.publisher.send("coordinator", "interpreter", "decision.requested",
                         {"gate_id": gate_id, "subject_id": bid,
                          "reason": "worker produced no on-stream reply"})
    swarm.pump()
    assert swarm.behaviour(bid).state == BehaviourState.BLOCKED
    return gate_id


def _retry(swarm: MiniSwarm, bid: str, gate_id: str) -> None:
    swarm.publisher.send("interpreter", "coordinator", "decision.made",
                         {"gate_id": gate_id, "subject_id": bid, "decision": "retry",
                          "comment": f"retry {bid}"})
    swarm.pump()


def test_a_story_retried_one_behaviour_at_a_time_still_converges(
    client, publisher
) -> None:
    """The friend-positions freeze, replayed end to end."""
    swarm = _start(client, publisher)
    # the limit wedged every worker, so every behaviour of I2 escalated —
    # the iteration's own integration slice included
    gates = {bid: _escalate(swarm, bid, s)
             for bid, s in ((B1, "V"), (B2, "W"), (INT, "X"), (ITER_INT, "Y"))}

    # the Owner answers B1 first, so it re-enters alone
    _retry(swarm, B1, gates[B1])
    assert swarm.behaviour(B1).state == BehaviourState.SPEC_DISPATCHED

    # ...and the siblings seconds later, into a budget B1 already fills.
    # The deadlock was exactly here: neither was ever dispatched again.
    _retry(swarm, B2, gates[B2])
    _retry(swarm, INT, gates[INT])
    for bid in (B2, INT):
        assert swarm.behaviour(bid).state == BehaviourState.SPEC_DISPATCHED, bid

    _go_red(swarm, B1)
    assert swarm.behaviour(B1).state == BehaviourState.RED_VERIFIED
    assert swarm.sent("build.requested") == [], "half a story is not a story"

    _go_red(swarm, B2)
    _go_red(swarm, INT)
    (build,) = swarm.sent("build.requested")
    assert [b["behaviour_id"] for b in build.payload["behaviours"]] == [B1, B2, INT]


def test_the_wip_throttle_still_holds_against_a_separate_unit(
    client, publisher
) -> None:
    """The deadlock fix must not become 'dispatch everything'.

    Rejoining an open story is free (that is the fix, exercised end to end by
    the test above). I1.INT is a unit of its own — no story_id — so it must
    still queue behind I1.S1: a fix that quietly let everything through would
    be a different bug.
    """
    swarm = _start(client, publisher)
    _go_red(swarm, B1)
    assert swarm.behaviour(B1).state == BehaviourState.RED_VERIFIED

    outsider = swarm.behaviour(ITER_INT)
    assert outsider.story_id is None
    assert outsider.state == BehaviourState.PLANNED, "the budget is still spent"


def test_wip_counts_behaviours_again_at_behaviour_granularity(
    client, publisher
) -> None:
    """The unit follows the policy: nothing changes for a per-slice swarm."""
    swarm = _start(client, publisher, policy=Policy())
    assert swarm.behaviour(B1).state == BehaviourState.SPEC_DISPATCHED
    for bid in (B2, INT):
        assert swarm.behaviour(bid).state == BehaviourState.PLANNED, bid

    _go_red(swarm, B1)
    (build,) = swarm.sent("build.requested")
    assert build.payload["behaviour_id"] == B1, "one slice at a time"
    assert "behaviours" not in build.payload
    assert swarm.behaviour(B2).state == BehaviourState.PLANNED


def test_a_fresh_dispatch_is_not_supervised_before_its_echo_folds(
    client, publisher
) -> None:
    """22 of friend-positions' 110 spec.requested were the same request twice,
    milliseconds apart — one duplicated specifier turn each, at story rates.

    react() mirrors a dispatch in memory and reads its own echo back on the
    NEXT loop; only that fold stamps the deadline clock. tick() runs in
    between. A behaviour that had sat PLANNED since the roadmap landed
    therefore carried an hours-old timestamp into SPEC_DISPATCHED, looked
    instantly overdue, and was re-sent before the specifier had seen it once.
    """
    swarm = _start(client, publisher)
    (first,) = swarm.sent("spec.requested")
    # white-box on purpose: pump() folds to a fixpoint, so the one-loop window
    # between react()'s mirror and its echo cannot be reached from outside
    b = swarm.behaviour(B1)
    b.state = BehaviourState.SPEC_DISPATCHED       # mirrored, echo not folded
    b.folded_state = str(BehaviourState.PLANNED)
    b.state_since = "2020-01-01T00:00:00+00:00"    # the clock of the OLD state

    swarm.dispatcher.tick(swarm.state, _epoch(b.state_since) + 99_999)
    assert swarm.sent("spec.requested") == [first], "re-sent before it was read"


def test_the_echo_having_folded_supervision_resumes(client, publisher) -> None:
    """The guard waits for the ledger, it does not disable the deadline."""
    swarm = _start(client, publisher)
    b = swarm.behaviour(B1)
    assert b.state == BehaviourState.SPEC_DISPATCHED
    assert str(b.state) == b.folded_state, "pump() folded the echo"

    swarm.dispatcher.tick(swarm.state, _epoch(b.state_since) + 2701)
    resent = [e for e in swarm.sent("spec.requested") if e.behaviour_id == B1]
    assert len(resent) == 2, "genuinely overdue work is still re-dispatched"
