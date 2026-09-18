"""The worker's task is the decision, and a late answer is still an answer.

Replay of the I3.INT stall. The Owner's `fix` came with a comment setting its
scope. The rework dispatched five milliseconds later carried the findings but
not the comment, so the builder never saw the scope. When the rework then ran
past its deadline, the re-dispatch was a plain build request pointing at the
integration test's original commit: the findings and the attempt were gone, and
the builder dutifully did an old job. The escalation that followed blocked the
behaviour, so the builder's real answer to the rework, arriving hours later
for the current attempt, was dropped, while a reply to the obsolete dispatch
arrived beside it.

What must hold instead:
- the decision's rationale travels in the rework as its controlling instruction;
- a rework that times out is re-sent as the SAME rework, never as a new build;
- a reply to the current attempt that arrives after escalation is kept, the
  latest correction winning over a placeholder, and the Owner's retry sends it
  to be verified rather than discarding it or accepting it outright;
- a reply to a superseded dispatch or attempt never moves the behaviour.
"""

from __future__ import annotations

import time

from test_coordinator import SHA_BUILD, MiniSwarm
from test_finding_lifecycle import PRESERVED, SIX, _answer, _judge, _to_security_gate

from relay.coordinator.model import BehaviourState

DECISION_COMMENT = PRESERVED[13719]["payload"]["comment"]
PLACEHOLDER = PRESERVED[13858]["payload"]
CORRECTION = PRESERVED[13859]["payload"]
SHA_LATE = "f" * 40


def _fixed(client, publisher) -> MiniSwarm:
    swarm = _to_security_gate(client, publisher)
    _judge(swarm, "fail", SIX)
    _answer(swarm, "I1", "fix", comment=DECISION_COMMENT)
    return swarm


def _overdue(swarm: MiniSwarm) -> None:
    """Let the rework's deadline pass once: supervision acts, then folds."""
    later = time.time() + 10 * swarm.dispatcher._policy.dispatch_timeout_s
    swarm.dispatcher.tick(swarm.state, later)
    swarm.pump()


def _escalated(client, publisher) -> MiniSwarm:
    swarm = _fixed(client, publisher)
    _overdue(swarm)                        # re-dispatch
    _overdue(swarm)                        # escalation
    assert swarm.behaviour("I1.INT").state == BehaviourState.BLOCKED
    return swarm


def _reply(swarm: MiniSwarm, reply_to: str, attempt: int, summary: str,
           sha: str = SHA_LATE) -> str:
    result = swarm.publisher.send(
        "builder", "coordinator", "behaviour.built",
        {"behaviour_id": "I1.INT", "story_id": None, "iteration_id": "I1",
         "commit_sha": sha, "attempt": attempt, "summary": summary},
        in_reply_to=reply_to)
    swarm.pump()
    return result.event_id


# ── the decision reaches the task ────────────────────────────────────────────

def test_the_decision_rationale_travels_with_the_rework(client, publisher) -> None:
    swarm = _fixed(client, publisher)
    (rework,) = swarm.sent("rework.requested")
    assert rework.payload["instruction"] == DECISION_COMMENT


def test_a_timed_out_rework_is_resent_as_the_same_rework(client, publisher) -> None:
    swarm = _fixed(client, publisher)
    builds_before = len(swarm.sent("build.requested"))
    _overdue(swarm)
    assert len(swarm.sent("build.requested")) == builds_before
    first, again = swarm.sent("rework.requested")
    assert again.payload == first.payload


# ── a late answer to the current attempt ─────────────────────────────────────

def test_a_late_reply_after_escalation_is_kept_not_dropped(client, publisher) -> None:
    swarm = _escalated(client, publisher)
    rework = swarm.sent("rework.requested")[0]
    attempt = rework.payload["attempt"]
    _reply(swarm, rework.event_id, attempt, PLACEHOLDER["summary"])
    correction = _reply(swarm, rework.event_id, attempt, CORRECTION["summary"])
    b = swarm.behaviour("I1.INT")
    assert b.state == BehaviourState.BLOCKED       # never advanced by itself
    assert b.late_completion is not None
    assert b.late_completion["event_id"] == correction
    assert b.late_completion["summary"] == CORRECTION["summary"]


def test_retry_sends_the_late_reply_to_be_verified(client, publisher) -> None:
    swarm = _escalated(client, publisher)
    rework = swarm.sent("rework.requested")[0]
    _reply(swarm, rework.event_id, rework.payload["attempt"], CORRECTION["summary"])
    runs_before = len(swarm.sent("run.requested"))
    _answer(swarm, "I1.INT", "retry")
    b = swarm.behaviour("I1.INT")
    assert b.built_commit == SHA_LATE
    assert b.state == BehaviourState.AT_RUN_PENDING
    run = swarm.sent("run.requested")[-1]
    assert len(swarm.sent("run.requested")) == runs_before + 1
    assert run.payload["commit_sha"] == SHA_LATE
    assert b.late_completion is None               # consumed once, not reusable


# ── a reply to something superseded ──────────────────────────────────────────

def test_a_reply_to_an_older_attempt_never_moves_the_current_one(client, publisher) -> None:
    swarm = _fixed(client, publisher)
    original = swarm.sent("build.requested")[-1]    # I1.INT's first build
    stale = _reply(swarm, original.event_id, 1, "already satisfied", sha=SHA_BUILD)
    b = swarm.behaviour("I1.INT")
    assert b.state == BehaviourState.BUILD_DISPATCHED
    assert stale in b.stale_replies


def test_a_stale_reply_after_escalation_is_not_mistaken_for_the_answer(
        client, publisher) -> None:
    swarm = _escalated(client, publisher)
    original = swarm.sent("build.requested")[-1]
    stale = _reply(swarm, original.event_id, 1, "already satisfied", sha=SHA_BUILD)
    b = swarm.behaviour("I1.INT")
    assert b.late_completion is None
    assert stale in b.stale_replies


# ── a build that follows a test rework starts afresh ─────────────────────────

def test_a_build_after_a_test_rework_accepts_the_builder_s_reply(client, publisher) -> None:
    """Replay of scopa I1.S1.B2 and prova I3.S7.B2: the specifier reworked
    the test (attempt 2), then a plain build request, which names no attempt,
    went to the builder, who echoed attempt 1. That reply answers the build it
    names and must move the behaviour."""
    from test_coordinator import SHA_SPEC

    swarm = MiniSwarm(client, publisher)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": __import__("test_coordinator").ROADMAP,
                    "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    swarm.pump()
    b = swarm.behaviour("I1.S1.B1")
    publisher.send("coordinator", "specifier", "rework.requested",
                   {"behaviour_id": "I1.S1.B1", "attempt": 2,
                    "findings": [{"title": "tautology", "detail": "passes with code deleted"}]},
                   behaviour_id="I1.S1.B1")
    swarm.pump()
    publisher.send("specifier", "coordinator", "spec.written",
                   {"behaviour_id": "I1.S1.B1", "test_paths": ["tests/acceptance/t.py"],
                    "commit_sha": SHA_SPEC, "touches": []})
    swarm.pump()
    red = swarm.sent("run.requested")[-1]
    publisher.send("toolgate", "coordinator", "run.completed",
                   {"run_id": red.payload["run_id"], "kind": "acceptance_test",
                    "commit_sha": SHA_SPEC, "exit_code": 1, "duration_s": 1.0,
                    "output_digest": "d" * 64})
    swarm.pump()
    build = swarm.sent("build.requested")[-1]
    assert b.state == BehaviourState.BUILD_DISPATCHED
    reply = publisher.send("builder", "coordinator", "behaviour.built",
                           {"behaviour_id": "I1.S1.B1", "story_id": "I1.S1",
                            "iteration_id": "I1", "commit_sha": SHA_BUILD, "attempt": 1},
                           in_reply_to=build.event_id)
    swarm.pump()
    assert reply.event_id not in b.stale_replies
    assert b.built_commit == SHA_BUILD


def test_drop_discards_a_kept_late_reply(client, publisher) -> None:
    swarm = _escalated(client, publisher)
    rework = swarm.sent("rework.requested")[0]
    _reply(swarm, rework.event_id, rework.payload["attempt"], CORRECTION["summary"])
    _answer(swarm, "I1.INT", "drop")
    b = swarm.behaviour("I1.INT")
    assert b.state == BehaviourState.DONE
    assert b.late_completion is None
