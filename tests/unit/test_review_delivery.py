"""Review delivery is independent of a model's ability to reach the ledger."""

import json
from pathlib import Path

import pytest

from relay.contract.envelope import Envelope
from relay.runners.base import RunnerCaps, TurnResult
from relay.runners.fake import FakeRunner
from relay.workers.chain import ChainWorker

GATE = "gate-01J5AB3CDEF4GH5JK6MN7PQ8R1"
SHA = "b" * 40


def worker_and_request(
    client,
    publisher,
    tmp_path,
    monkeypatch,
    result,
    role="reviewer",
    gate="code_review",
):
    runner = FakeRunner(lambda *_: result)
    runner.capabilities = RunnerCaps(supports_resume=True, structured_reviews=True)
    worker = ChainWorker(
        "testswarm",
        role,
        runner,
        Path("roles/reviewer.md"),
        tmp_path,
        tmp_path / "state",
        client,
    )
    monkeypatch.setattr(
        worker,
        "_handle_in_pinned_worktree",
        lambda env: worker._run_turn_loop(env, tmp_path),
    )
    publisher.send(
        "coordinator",
        role,
        "gate.requested",
        {
            "gate_id": GATE,
            "gate": gate,
            "subject_kind": "behaviour",
            "subject_id": "I1.S1.B1",
            "commit_sha": SHA,
            "base_sha": "a" * 40,
        },
        behaviour_id="I1.S1.B1",
        story_id="I1.S1",
        iteration_id="I1",
    )
    return worker, Envelope.from_fields(client.xrange("relay:testswarm:ledger")[-1][1])


def events(client, kind):
    return [
        env
        for _, fields in client.xrange("relay:testswarm:ledger")
        if (env := Envelope.from_fields(fields)).type == kind
    ]


def review(verdict="fail"):
    return {
        "gate_id": GATE,
        "verdict": verdict,
        "summary": "The empty input path loses data.",
        "limitations": ["Tests were inspected, not executed."],
        "findings": [
            {
                "severity": "major",
                "title": "Empty input deletes records",
                "detail": "An empty input reaches delete_all(). Preserve stored records.",
                "file": "store.py",
                "line": 42,
            }
        ],
    }


def test_review_reaches_coordinator_and_interpreter_once(
    client, publisher, tmp_path, monkeypatch
):
    payload = review()
    worker, request = worker_and_request(
        client,
        publisher,
        tmp_path,
        monkeypatch,
        TurnResult(ok=True, text=json.dumps(payload), model="review-model"),
    )
    worker.handle(request)
    worker.handle(request)
    verdicts = events(client, "gate.judged")
    assert len(verdicts) == 1
    assert verdicts[0].payload == payload
    assert verdicts[0].in_reply_to == request.event_id
    reports = events(client, "update.shared")
    assert len(reports) == 1
    assert reports[0].to_role == "interpreter"
    assert reports[0].in_reply_to == verdicts[0].event_id
    assert "Empty input deletes records" in reports[0].payload["text"]
    assert "Tests were inspected, not executed." in reports[0].payload["text"]
    assert SHA in reports[0].payload["text"]
    assert len(worker.runner.turns) == 1


@pytest.mark.parametrize(
    "result",
    [
        TurnResult(ok=True, text="Looks good!"),
        TurnResult(ok=False, text=json.dumps(review()), error="process failed"),
        TurnResult(ok=True, text=json.dumps({**review(), "gate_id": GATE[:-1] + "2"})),
        TurnResult(ok=True, text=json.dumps(review("pass"))),
        TurnResult(ok=True, text=json.dumps({**review(), "findings": []})),
    ],
)
def test_invalid_or_failed_review_never_becomes_a_verdict(
    client, publisher, tmp_path, monkeypatch, result
):
    worker, request = worker_and_request(
        client, publisher, tmp_path, monkeypatch, result
    )
    worker.handle(request)
    assert not events(client, "gate.judged")
    assert not events(client, "update.shared")
    assert events(client, "worker.failed")


def test_restart_forwards_existing_verdict_without_rerunning_model(
    client, publisher, tmp_path, monkeypatch
):
    worker, request = worker_and_request(
        client, publisher, tmp_path, monkeypatch, "must not run"
    )
    publisher.send(
        "reviewer",
        "coordinator",
        "gate.judged",
        {"gate_id": GATE, "verdict": "pass", "findings": []},
        in_reply_to=request.event_id,
    )
    worker.handle(request)
    assert len(events(client, "update.shared")) == 1
    assert not worker.runner.turns


def test_a_clean_review_reports_its_limits_without_inventing_findings(
    client, publisher, tmp_path, monkeypatch
):
    payload = {
        **review("pass"),
        "findings": [],
        "summary": "No blocking findings in this diff.",
    }
    worker, request = worker_and_request(
        client, publisher, tmp_path, monkeypatch, json.dumps(payload)
    )
    worker.handle(request)
    assert events(client, "gate.judged")[0].payload["verdict"] == "pass"
    assert "No blocking findings" in events(client, "update.shared")[0].payload["text"]
