"""The optional Codex gate is independent of the existing review gate."""

import json

import pytest

from relay.coordinator.model import BehaviourState
from relay.coordinator.policy import GateSpec, Policy
from relay.runners.claude import ClaudeRunner
from relay.runners.codex import CodexRunner
from relay.workers.run import _runner_for
from test_gates import _start, _spec_and_build
from test_review_delivery import worker_and_request, review, events


@pytest.mark.parametrize("first", ["reviewer", "codex_reviewer"])
def test_both_reviews_are_dispatched_together_and_required(client, publisher, first):
    policy = Policy(
        per_behaviour=(
            GateSpec("code_review", "reviewer"),
            GateSpec("codex_review", "codex_reviewer"),
        )
    )
    swarm = _start(client, publisher, policy)
    _spec_and_build(swarm, "I1.S1.B1")
    requests = {event.to_role: event for event in swarm.sent("gate.requested")}
    assert set(requests) == {"reviewer", "codex_reviewer"}
    assert len({e.payload["commit_sha"] for e in requests.values()}) == 1
    for index, role in enumerate([first, next(r for r in requests if r != first)]):
        publisher.send(
            role,
            "coordinator",
            "gate.judged",
            {
                "gate_id": requests[role].payload["gate_id"],
                "verdict": "pass",
                "findings": [],
            },
            in_reply_to=requests[role].event_id,
        )
        swarm.pump()
        if index == 0:
            assert swarm.behaviour("I1.S1.B1").state == BehaviourState.GATES_PENDING
            assert not swarm.sent("judgement.requested")
    assert swarm.sent("judgement.requested")


def test_optional_codex_role_does_not_replace_claude_reviewer(tmp_path):
    assert isinstance(_runner_for("reviewer", {}, tmp_path), ClaudeRunner)
    extra = _runner_for("codex_reviewer", {}, tmp_path)
    assert isinstance(extra, CodexRunner)
    assert extra.sandbox == "read-only"


def test_codex_report_uses_its_own_identity(client, publisher, tmp_path, monkeypatch):
    worker, request = worker_and_request(
        client,
        publisher,
        tmp_path,
        monkeypatch,
        json.dumps(review()),
        role="codex_reviewer",
        gate="codex_review",
    )
    worker.handle(request)
    worker.handle(request)
    assert len(events(client, "gate.judged")) == 1
    assert events(client, "gate.judged")[0].from_role == "codex_reviewer"
    reports = events(client, "update.shared")
    assert len(reports) == 1
    assert reports[0].from_role == "codex_reviewer"
    assert "codex_reviewer" in reports[0].payload["text"]


def test_existing_reviewer_cannot_approve_the_codex_gate(client, publisher):
    policy = Policy(
        per_behaviour=(
            GateSpec("code_review", "reviewer"),
            GateSpec("codex_review", "codex_reviewer"),
        )
    )
    swarm = _start(client, publisher, policy)
    _spec_and_build(swarm, "I1.S1.B1")
    request = next(
        e for e in swarm.sent("gate.requested") if e.to_role == "codex_reviewer"
    )
    publisher.send(
        "reviewer",
        "coordinator",
        "gate.judged",
        {
            "gate_id": request.payload["gate_id"],
            "verdict": "pass",
            "findings": [],
        },
    )
    swarm.pump()
    assert (
        swarm.behaviour("I1.S1.B1").pending_gates[request.payload["gate_id"]].verdict
        is None
    )
