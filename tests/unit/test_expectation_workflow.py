"""The analyst's questions exist before a builder can start."""

import pytest


from copy import deepcopy

from test_coordinator import ROADMAP, MiniSwarm


def start(client, publisher):
    roadmap = deepcopy(ROADMAP)
    roadmap["protocol_version"] = 2
    source = publisher.send(
        "owner", "interpreter", "problem.stated", {"text": "Show only free rooms."}
    )
    publisher.send(
        "interpreter",
        "coordinator",
        "roadmap.committed",
        {"roadmap": roadmap, "intake": {"mode": "greenfield"}},
    )
    publisher.send(
        "interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"}
    )
    swarm = MiniSwarm(client, publisher)
    swarm.pump()
    return swarm, source.event_id


def test_story_preparation_precedes_specification_and_build(client, publisher):
    swarm, source = start(client, publisher)
    assert not swarm.sent("spec.requested")
    request = swarm.sent("story.preparation.requested")[-1]
    assert request.payload["sources"][0]["event_id"] == source
    assert request.payload["version"] == 1
    assert not swarm.sent("build.requested")


def prepare(swarm, source):
    request = swarm.sent("story.preparation.requested")[-1]
    payload = {
        "story_id": "I1.S1",
        "version": 1,
        "expectations": [
            {
                "id": "I1.S1.B1",
                "text": ROADMAP["iterations"][0]["stories"][0]["acceptance_criteria"][
                    0
                ]["text"],
                "source_refs": [{"event_id": source, "quote": "free rooms"}],
            }
        ],
        "properties": [
            {
                "id": "I1.S1.P1",
                "question": "Are booked rooms always excluded?",
                "scope": "All booking states",
                "expectation_ids": ["I1.S1.B1"],
                "verification": "Generate booking states and check the CLI output",
            }
        ],
    }
    swarm.publisher.send(
        "analyst",
        "coordinator",
        "story.prepared",
        payload,
        in_reply_to=request.event_id,
    )
    swarm.pump()
    return payload


def test_questions_and_checks_are_reviewed_before_the_builder_can_start(
    client, publisher
):
    swarm, source = start(client, publisher)
    payload = prepare(swarm, source)
    assert not swarm.sent("spec.requested")
    request = swarm.sent("story.verification.requested")[-1]
    assert request.payload["properties"] == payload["properties"]
    publisher.send(
        "specifier",
        "coordinator",
        "story.verification.written",
        {
            "story_id": "I1.S1",
            "version": 1,
            "commit_sha": "b" * 40,
            "checks": {"I1.S1.P1": ["tests/properties/test_rooms.py::test_booked"]},
        },
        in_reply_to=request.event_id,
    )
    swarm.pump()
    assert not swarm.sent("spec.requested")
    review = swarm.sent("story.verification.review.requested")[-1]
    publisher.send(
        "qa",
        "coordinator",
        "story.verification.reviewed",
        {
            "story_id": "I1.S1",
            "version": 1,
            "verdict": "pass",
            "reason": "The checks exercise the public CLI",
        },
        in_reply_to=review.event_id,
    )
    swarm.pump()
    assert swarm.sent("spec.requested")


def ready(client, publisher):
    swarm, source = start(client, publisher)
    prepare(swarm, source)
    request = swarm.sent("story.verification.requested")[-1]
    publisher.send(
        "specifier",
        "coordinator",
        "story.verification.written",
        {
            "story_id": "I1.S1",
            "version": 1,
            "commit_sha": "b" * 40,
            "checks": {"I1.S1.P1": ["tests/properties/test_rooms.py::test_booked"]},
        },
        in_reply_to=request.event_id,
    )
    swarm.pump()
    review = swarm.sent("story.verification.review.requested")[-1]
    publisher.send(
        "qa",
        "coordinator",
        "story.verification.reviewed",
        {
            "story_id": "I1.S1",
            "version": 1,
            "verdict": "pass",
            "reason": "Independent checks",
        },
        in_reply_to=review.event_id,
    )
    swarm.pump()
    return swarm


def complete_evidence(swarm, monkeypatch):
    import hashlib
    from relay import receipts

    request = [
        r for r in swarm.sent("run.requested") if r.payload["kind"] == "evidence"
    ][-1]
    declaration = request.payload["evidence"]
    reports = [
        {"id": s, "outcome": "passed"}
        for selectors in declaration["bindings"].values()
        for s in selectors
    ]
    receipt = {
        "schema_version": 1,
        "swarm": "testswarm",
        "request_id": request.event_id,
        "run_id": request.payload["run_id"],
        "story_id": "I1.S1",
        "version": declaration["version"],
        "commit_sha": request.payload["commit_sha"],
        "declaration_digest": receipts.digest(declaration),
        "checks": reports,
        "coverage": receipts.coverage(declaration["bindings"], reports),
        "stdout": "executed",
        "stderr": "",
        "exit_code": 0,
    }
    swarm.publisher.send(
        "toolgate",
        "coordinator",
        "run.completed",
        {
            "run_id": request.payload["run_id"],
            "kind": "evidence",
            "commit_sha": request.payload["commit_sha"],
            "exit_code": 0,
            "duration_s": 1,
            "output_digest": hashlib.sha256(b"executed").hexdigest(),
            "receipt": receipt,
        },
        in_reply_to=request.event_id,
        story_id="I1.S1",
        iteration_id="I1",
    )
    swarm.pump()
    return request


def test_story_requires_fresh_executed_evidence_and_every_original_answer(
    client, publisher, monkeypatch
):
    from test_coordinator import _drive_behaviour_to_done

    swarm = ready(client, publisher)
    _drive_behaviour_to_done(swarm, "I1.S1.B1")
    _drive_behaviour_to_done(swarm, "I1.S1.INT")
    assert not swarm.sent("story.completed")
    evidence = complete_evidence(swarm, monkeypatch)
    request = swarm.sent("story.validation.requested")[-1]
    assert (
        request.payload["properties"][0]["question"]
        == "Are booked rooms always excluded?"
    )
    publisher.send(
        "analyst",
        "coordinator",
        "story.validation.judged",
        {
            "story_id": "I1.S1",
            "version": 1,
            "commit_sha": evidence.payload["commit_sha"],
            "run_id": evidence.payload["run_id"],
            "answers": [
                {
                    "property_id": "I1.S1.P1",
                    "verdict": "supported",
                    "run_id": evidence.payload["run_id"],
                    "explanation": "Every executed state excludes booked rooms",
                }
            ],
        },
        in_reply_to=request.event_id,
    )
    swarm.pump()
    assert len(swarm.sent("story.completed")) == 1
    fresh = MiniSwarm(client, publisher)
    fresh.pump()
    assert len(fresh.sent("story.completed")) == 1


def test_invalid_source_or_changed_expectation_cannot_be_prepared(client, publisher):
    swarm, source = start(client, publisher)
    payload = prepare(swarm, source)
    # An unsolicited replacement cannot weaken the approved expectation.
    payload["expectations"][0]["text"] = "Any room may be listed"
    publisher.send(
        "analyst",
        "coordinator",
        "story.prepared",
        payload,
        in_reply_to=swarm.sent("story.preparation.requested")[-1].event_id,
    )
    swarm.pump()
    assert swarm.state.evidence_rejections
    assert (
        swarm.state.stories["I1.S1"].evidence.expectations[0]["text"]
        != "Any room may be listed"
    )


def test_fabricated_execution_cannot_complete_a_story(client, publisher, monkeypatch):
    from test_coordinator import _drive_behaviour_to_done

    swarm = ready(client, publisher)
    _drive_behaviour_to_done(swarm, "I1.S1.B1")
    _drive_behaviour_to_done(swarm, "I1.S1.INT")
    request = [
        r for r in swarm.sent("run.requested") if r.payload["kind"] == "evidence"
    ][-1]
    publisher.send(
        "toolgate",
        "coordinator",
        "run.completed",
        {
            "run_id": request.payload["run_id"],
            "kind": "evidence",
            "commit_sha": request.payload["commit_sha"],
            "exit_code": 0,
            "duration_s": 1,
            "output_digest": "e" * 64,
            "receipt": {"stdout": "looks good"},
            "signature": "invented",
        },
        story_id="I1.S1",
        iteration_id="I1",
        in_reply_to=request.event_id,
    )
    swarm.pump()
    assert not swarm.sent("story.completed")
    assert not swarm.sent("story.validation.requested")
    assert swarm.state.stories["I1.S1"].escalated


def test_story_dispatch_timeout_is_bounded_and_replay_preserves_attempts(
    client, publisher
):
    import time

    swarm, _ = start(client, publisher)
    for attempt in range(4):
        swarm.dispatcher.tick(
            swarm.state, time.time() + swarm.dispatcher._policy.dispatch_timeout_s + 10
        )
        swarm.pump()
    assert (
        len(swarm.sent("story.preparation.requested"))
        == swarm.dispatcher._policy.max_attempts
    )
    assert swarm.state.stories["I1.S1"].escalated
    fresh = MiniSwarm(client, publisher)
    fresh.pump()
    assert (
        len(fresh.sent("story.preparation.requested"))
        == swarm.dispatcher._policy.max_attempts
    )


def built_story(client, publisher, monkeypatch):
    from test_coordinator import _drive_behaviour_to_done

    swarm = ready(client, publisher)
    _drive_behaviour_to_done(swarm, "I1.S1.B1")
    _drive_behaviour_to_done(swarm, "I1.S1.INT")
    complete_evidence(swarm, monkeypatch)
    return swarm


def answer(swarm, verdict="supported", **changes):
    request = swarm.sent("story.validation.requested")[-1]
    payload = {
        "story_id": "I1.S1",
        "version": request.payload["version"],
        "commit_sha": request.payload["commit_sha"],
        "run_id": request.payload["run_id"],
        "answers": [
            {
                "property_id": "I1.S1.P1",
                "verdict": verdict,
                "run_id": request.payload["run_id"],
                "explanation": "Check the booking boundary",
            }
        ],
        **changes,
    }
    swarm.publisher.send(
        "analyst",
        "coordinator",
        "story.validation.judged",
        payload,
        in_reply_to=request.event_id,
    )
    swarm.pump()


@pytest.mark.parametrize(
    "change",
    [
        {"commit_sha": "f" * 40},
        {"run_id": "run-01J5AB3CDEF4GH5JK6MN7PQ8RS"},
        {
            "answers": [
                {
                    "property_id": "I1.S1.P99",
                    "verdict": "supported",
                    "run_id": "run-01J5AB3CDEF4GH5JK6MN7PQ8RS",
                    "explanation": "yes",
                }
            ]
        },
    ],
)
def test_wrong_or_stale_answers_do_not_complete(client, publisher, monkeypatch, change):
    swarm = built_story(client, publisher, monkeypatch)
    answer(swarm, **change)
    assert not swarm.sent("story.completed")
    assert swarm.state.evidence_rejections


def test_contradicted_property_returns_real_findings_to_builder(
    client, publisher, monkeypatch
):
    swarm = built_story(client, publisher, monkeypatch)
    answer(swarm, "contradicted")
    rework = swarm.sent("rework.requested")[-1]
    assert rework.to_role == "builder"
    assert "booking boundary" in rework.payload["findings"][0]["detail"]
    assert not swarm.sent("story.completed")


def test_insufficient_evidence_returns_to_specifier_without_weakening_questions(
    client, publisher, monkeypatch
):
    swarm = built_story(client, publisher, monkeypatch)
    original = swarm.state.stories["I1.S1"].evidence.properties.copy()
    answer(swarm, "insufficient_evidence")
    request = swarm.sent("story.verification.requested")[-1]
    assert request.payload["properties"] == original
    assert len(swarm.sent("story.verification.requested")) == 2
    assert not swarm.sent("story.completed")


def test_final_iteration_commit_rechecks_previously_accepted_story(
    client, publisher, monkeypatch
):
    swarm = built_story(client, publisher, monkeypatch)
    answer(swarm)
    # The integration spec is already satisfied at a later commit.
    publisher.send(
        "specifier",
        "coordinator",
        "spec.written",
        {
            "behaviour_id": "I1.INT",
            "test_paths": ["tests/integration.py"],
            "commit_sha": "f" * 40,
            "touches": [],
        },
    )
    swarm.pump()
    request = swarm.sent("run.requested")[-1]
    publisher.send(
        "toolgate",
        "coordinator",
        "run.completed",
        {
            "run_id": request.payload["run_id"],
            "kind": "acceptance_test",
            "commit_sha": "f" * 40,
            "exit_code": 0,
            "duration_s": 1,
            "output_digest": "e" * 64,
        },
        in_reply_to=request.event_id,
    )
    swarm.pump()
    assert not swarm.sent("iteration.finished")
    latest = [
        r for r in swarm.sent("run.requested") if r.payload["kind"] == "evidence"
    ][-1]
    assert latest.payload["commit_sha"] == "f" * 40
    assert "I1.INT" in latest.payload["evidence"]["bindings"]
    complete_evidence(swarm, monkeypatch)
    answer(swarm)
    assert swarm.sent("iteration.finished")


def test_conflicting_receipt_revokes_existing_story_acceptance(
    client, publisher, monkeypatch
):
    swarm = built_story(client, publisher, monkeypatch)
    answer(swarm)
    completed = [
        r for r in swarm.sent("run.completed") if r.payload["kind"] == "evidence"
    ][-1]
    publisher.send(
        "toolgate",
        "coordinator",
        "run.completed",
        {**completed.payload, "exit_code": 1},
        in_reply_to=completed.in_reply_to,
        story_id="I1.S1",
        iteration_id="I1",
    )
    swarm.pump()
    story = swarm.state.stories["I1.S1"]
    assert not story.evidence.accepted_commit
    assert not story.done_announced


def test_approved_replan_requires_fresh_obligations_and_completion(
    client, publisher, monkeypatch
):
    swarm = built_story(client, publisher, monkeypatch)
    answer(swarm)
    assert swarm.state.stories["I1.S1"].evidence.accepted_commit
    roadmap = deepcopy(ROADMAP)
    roadmap["protocol_version"] = 2
    publisher.send(
        "interpreter",
        "coordinator",
        "roadmap.committed",
        {"roadmap": roadmap, "intake": {"mode": "greenfield"}},
    )
    publisher.send(
        "interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"}
    )
    swarm.pump()
    story = swarm.state.stories["I1.S1"]
    assert story.evidence.version == 2
    assert not story.evidence.ready
    assert not story.evidence.accepted_commit
    assert not story.done_announced
    assert swarm.sent("story.preparation.requested")[-1].payload["version"] == 2
