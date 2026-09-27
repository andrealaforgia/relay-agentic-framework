"""A correctly shaped reference is not evidence for the current work."""

import pytest

from test_coordinator import (
    ROADMAP,
    SHA_BUILD,
    SHA_SPEC,
    MiniSwarm,
    _drive_behaviour_to_done,
)
from test_gates import _spec_and_build

from relay.coordinator.model import BehaviourState
from relay.coordinator.projection import apply

BEHAVIOUR = "I1.S1.B1"
UNKNOWN_RUN = "run-01M08YB2FF5X6KHTRJXR948MD1"
UNKNOWN_EVENT = "01M08YB2FF5X6KHTRJXR948MD2"


@pytest.fixture
def swarm(client, publisher):
    swarm = MiniSwarm(client, publisher)
    publisher.send(
        "interpreter",
        "coordinator",
        "roadmap.committed",
        {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}},
    )
    publisher.send(
        "interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"}
    )
    swarm.pump()
    return swarm


def complete(swarm, request, *, exit_code=0, payload=None, **routing):
    result = {
        "run_id": request.payload["run_id"],
        "kind": request.payload["kind"],
        "commit_sha": request.payload["commit_sha"],
        "exit_code": exit_code,
        "duration_s": 0.1,
        "output_digest": "e" * 64,
        **(payload or {}),
    }
    swarm.publisher.send(
        "toolgate",
        "coordinator",
        "run.completed",
        result,
        **{"in_reply_to": request.event_id, **routing},
    )
    swarm.pump()


def judge(swarm, run_id, **routing):
    swarm.publisher.send(
        "specifier",
        "coordinator",
        "acceptance.judged",
        {"behaviour_id": BEHAVIOUR, "verdict": "pass", "run_id": run_id},
        **routing,
    )
    swarm.pump()


@pytest.mark.parametrize(
    "payload,routing",
    [
        ({"commit_sha": "f" * 40}, {}),
        ({"kind": "properties"}, {}),
        ({}, {"in_reply_to": UNKNOWN_EVENT}),
        ({}, {"behaviour_id": "I1.S1.B2"}),
        ({}, {"story_id": "I1.S2"}),
        ({}, {"iteration_id": "I2"}),
        ({}, {"commit_sha": "f" * 40}),
    ],
)
def test_mismatched_result_cannot_prove_red(swarm, payload, routing):
    swarm.publisher.send(
        "specifier",
        "coordinator",
        "spec.written",
        {
            "behaviour_id": BEHAVIOUR,
            "test_paths": ["tests/rooms.py"],
            "commit_sha": SHA_SPEC,
            "touches": [],
        },
    )
    swarm.pump()
    request = swarm.sent("run.requested")[-1]
    complete(swarm, request, exit_code=1, payload=payload, **routing)

    assert swarm.behaviour(BEHAVIOUR).state == BehaviourState.RED_PENDING
    assert not swarm.sent("build.requested")
    assert swarm.state.runs[request.payload["run_id"]].exit_code is None
    assert swarm.sent("decision.requested"), "rejected evidence must be visible"


@pytest.mark.parametrize("citation", ["unknown", "red"])
def test_acceptance_requires_the_requested_green_run(swarm, citation):
    _spec_and_build(swarm, BEHAVIOUR)
    run_id = (
        UNKNOWN_RUN
        if citation == "unknown"
        else swarm.sent("run.requested")[0].payload["run_id"]
    )
    judge(swarm, run_id)
    assert swarm.behaviour(BEHAVIOUR).state == BehaviourState.ACCEPTANCE_PENDING


def test_acceptance_reply_must_match_the_judgement_dispatch(swarm):
    _spec_and_build(swarm, BEHAVIOUR)
    request = swarm.sent("judgement.requested")[-1]
    judge(swarm, request.payload["run_id"], in_reply_to=UNKNOWN_EVENT)
    assert swarm.behaviour(BEHAVIOUR).state == BehaviourState.ACCEPTANCE_PENDING


def test_identical_run_delivery_is_harmless_but_conflicting_result_cannot_pass(swarm):
    _spec_and_build(swarm, BEHAVIOUR)
    request = swarm.sent("run.requested")[-1]
    complete(swarm, request)
    assert swarm.behaviour(BEHAVIOUR).state == BehaviourState.ACCEPTANCE_PENDING
    assert not swarm.sent("decision.requested")

    complete(swarm, request, exit_code=1)
    judge(swarm, request.payload["run_id"])
    assert swarm.behaviour(BEHAVIOUR).state != BehaviourState.DONE
    assert swarm.sent("decision.requested")


def test_previous_attempt_cannot_be_accepted_even_when_the_commit_is_reused(
    swarm, client, publisher
):
    _spec_and_build(swarm, BEHAVIOUR)
    old = swarm.sent("judgement.requested")[-1]
    publisher.send(
        "specifier",
        "coordinator",
        "acceptance.judged",
        {
            "behaviour_id": BEHAVIOUR,
            "verdict": "fail",
            "run_id": old.payload["run_id"],
            "reason": "missing boundary check",
        },
    )
    swarm.pump()
    rework = swarm.sent("rework.requested")[-1]
    publisher.send(
        "builder",
        "coordinator",
        "behaviour.built",
        {
            "behaviour_id": BEHAVIOUR,
            "story_id": "I1.S1",
            "iteration_id": "I1",
            "commit_sha": SHA_BUILD,
            "attempt": rework.payload["attempt"],
        },
        in_reply_to=rework.event_id,
    )
    swarm.pump()
    complete(swarm, swarm.sent("run.requested")[-1])
    current = swarm.sent("judgement.requested")[-1]

    judge(swarm, old.payload["run_id"])
    assert swarm.behaviour(BEHAVIOUR).state == BehaviourState.ACCEPTANCE_PENDING
    judge(swarm, current.payload["run_id"], in_reply_to=current.event_id)
    assert swarm.behaviour(BEHAVIOUR).state == BehaviourState.DONE

    before = len(swarm.sent("judgement.requested"))
    fresh = MiniSwarm(client, publisher)
    fresh.pump()
    assert fresh.behaviour(BEHAVIOUR).state == BehaviourState.DONE
    assert len(fresh.sent("judgement.requested")) == before


def test_repeated_request_delivery_does_not_erase_completed_evidence(swarm):
    _spec_and_build(swarm, BEHAVIOUR)
    request = swarm.sent("run.requested")[-1]
    apply(swarm.state, request)
    assert swarm.state.runs[request.payload["run_id"]].exit_code == 0
    judge(swarm, request.payload["run_id"])
    assert swarm.behaviour(BEHAVIOUR).state == BehaviourState.DONE


@pytest.mark.parametrize(
    "routing",
    [
        {"behaviour_id": "I1.S1.B2"},
        {"story_id": "I1.S2"},
        {"iteration_id": "I2"},
        {"commit_sha": "f" * 40},
    ],
)
def test_acceptance_routing_cannot_contradict_its_evidence(swarm, routing):
    _spec_and_build(swarm, BEHAVIOUR)
    request = swarm.sent("judgement.requested")[-1]
    judge(swarm, request.payload["run_id"], **routing)
    assert swarm.behaviour(BEHAVIOUR).state == BehaviourState.ACCEPTANCE_PENDING


def test_conflicting_results_escalate_the_affected_behaviour_and_retry_recovers(swarm):
    _spec_and_build(swarm, BEHAVIOUR)
    complete(swarm, swarm.sent("run.requested")[-1], exit_code=1)
    decision = swarm.sent("decision.requested")[-1]
    assert decision.payload["subject_id"] == BEHAVIOUR
    swarm.publisher.send(
        "interpreter",
        "coordinator",
        "decision.made",
        {
            "gate_id": decision.payload["gate_id"],
            "subject_id": BEHAVIOUR,
            "decision": "retry",
        },
    )
    swarm.pump()
    # The existing WIP policy finishes the integration check dispatched while
    # this behaviour was blocked before restarting the retried behaviour.
    _drive_behaviour_to_done(swarm, "I1.S1.INT")
    _spec_and_build(swarm, BEHAVIOUR)
    judge(swarm, swarm.sent("judgement.requested")[-1].payload["run_id"])
    assert swarm.behaviour(BEHAVIOUR).state == BehaviourState.DONE


def test_late_result_cannot_satisfy_a_replacement_run(swarm):
    swarm.publisher.send(
        "specifier",
        "coordinator",
        "spec.written",
        {
            "behaviour_id": BEHAVIOUR,
            "test_paths": ["tests/rooms.py"],
            "commit_sha": SHA_SPEC,
            "touches": [],
        },
    )
    swarm.pump()
    original = swarm.sent("run.requested")[-1]
    swarm.publisher.send(
        "coordinator",
        "toolgate",
        "run.requested",
        {**original.payload, "run_id": UNKNOWN_RUN},
        behaviour_id=BEHAVIOUR,
        story_id="I1.S1",
        iteration_id="I1",
    )
    swarm.pump()
    replacement = swarm.sent("run.requested")[-1]
    complete(swarm, original, exit_code=1)
    assert not swarm.sent("build.requested")
    complete(swarm, replacement, exit_code=1)
    assert swarm.behaviour(BEHAVIOUR).state == BehaviourState.BUILD_DISPATCHED


@pytest.mark.parametrize(
    "kind,story_id",
    [
        ("setup", None),
        ("properties", None),
        ("properties", "I1.S1"),
        ("mutation", "I1.S1"),
    ],
)
def test_scoped_runs_reject_wrong_commits_and_escalate_conflicts(swarm, kind, story_id):
    swarm.publisher.send(
        "coordinator",
        "toolgate",
        "run.requested",
        {"run_id": UNKNOWN_RUN, "kind": kind, "commit_sha": SHA_BUILD},
        story_id=story_id,
        iteration_id="I1",
    )
    swarm.pump()
    request = swarm.sent("run.requested")[-1]
    complete(swarm, request, payload={"commit_sha": SHA_SPEC})
    assert swarm.state.runs[UNKNOWN_RUN].exit_code is None

    complete(swarm, request)
    complete(swarm, request, exit_code=1)
    decision = swarm.sent("decision.requested")[-1]
    assert decision.payload["subject_id"] == (story_id or "I1")
    subject = (
        swarm.state.stories[story_id] if story_id else swarm.state.iterations["I1"]
    )
    assert subject.escalated
    swarm.publisher.send(
        "interpreter",
        "coordinator",
        "decision.made",
        {
            "gate_id": decision.payload["gate_id"],
            "subject_id": story_id or "I1",
            "decision": "retry",
        },
    )
    swarm.pump()
    assert not subject.escalated
    if kind == "properties":
        assert subject.properties_run_id != UNKNOWN_RUN


def test_a_run_id_cannot_be_reassigned_after_execution(swarm):
    _spec_and_build(swarm, BEHAVIOUR)
    request = swarm.sent("run.requested")[-1]
    swarm.publisher.send(
        "coordinator",
        "toolgate",
        "run.requested",
        {**request.payload, "commit_sha": "f" * 40},
        behaviour_id=BEHAVIOUR,
    )
    swarm.pump()
    complete(swarm, swarm.sent("run.requested")[-1])
    assert swarm.state.runs[request.payload["run_id"]].commit_sha == SHA_BUILD
    assert swarm.sent("decision.requested")


def test_satisfied_behaviour_records_the_commit_actually_executed(swarm):
    swarm.publisher.send(
        "specifier",
        "coordinator",
        "spec.satisfied",
        {
            "behaviour_id": BEHAVIOUR,
            "test_paths": ["tests/guard.py"],
            "commit_sha": SHA_SPEC,
            "reason": "Behaviour arrived in earlier work",
        },
    )
    swarm.pump()
    request = swarm.sent("run.requested")[-1]
    complete(swarm, request)
    assert swarm.behaviour(BEHAVIOUR).built_commit == request.payload["commit_sha"]
