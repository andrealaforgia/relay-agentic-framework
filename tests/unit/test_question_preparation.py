"""Question authoring uses the read-only Codex transport, not Redis shell access."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from relay.ledger.reader import read_all
from relay.runners.base import RunnerCaps, TurnResult
from relay.runners.fake import FakeRunner
from relay.workers.chain import ChainWorker

EXAMPLES = yaml.safe_load(Path("contract/examples.yaml").read_text())


def prepare(client, publisher, tmp_path, payload, ok=True):
    runner = FakeRunner(
        lambda *_: TurnResult(
            ok=ok, text=json.dumps(payload), session_ref="codex-thread"
        )
    )
    runner.capabilities = RunnerCaps(supports_resume=True, structured_preparations=True)
    worker = ChainWorker(
        "testswarm",
        "analyst",
        FakeRunner(lambda *_: "wrong runner"),
        Path("roles/analyst.md"),
        tmp_path,
        tmp_path / "state",
        client,
        runners={"story.preparation.requested": runner},
    )
    publisher.send(
        "coordinator",
        "analyst",
        "story.preparation.requested",
        EXAMPLES["story.preparation.requested"],
        story_id="I1.S1",
        iteration_id="I1",
    )
    request = list(read_all(client, "testswarm"))[-1][1]
    worker.handle(request)
    worker.handle(request)
    events = [event for _, event in read_all(client, "testswarm")]
    return events, runner, request


def test_questions_are_published_once_with_original_request_identity(
    client, publisher, tmp_path
):
    payload = deepcopy(EXAMPLES["story.prepared"])
    events, runner, request = prepare(client, publisher, tmp_path, payload)
    prepared = [event for event in events if event.type == "story.prepared"]
    assert len(prepared) == 1
    assert prepared[0].payload == payload
    assert prepared[0].in_reply_to == request.event_id
    assert prepared[0].from_role == "analyst"
    assert len(runner.turns) == 1
    assert "Do not call relay-send" in runner.turns[0]


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_version",
        "changed_expectation",
        "invented_source",
        "wrong_story",
        "failed_process",
    ],
)
def test_invalid_question_preparation_cannot_start_implementation(
    client, publisher, tmp_path, fault
):
    payload = deepcopy(EXAMPLES["story.prepared"])
    if fault == "wrong_version":
        payload["version"] += 1
    elif fault == "changed_expectation":
        payload["expectations"][0]["text"] = "Any room may be listed."
    elif fault == "invented_source":
        payload["expectations"][0]["source_refs"][0]["quote"] = "invented source"
    elif fault == "wrong_story":
        payload["story_id"] = "I1.S2"
    events, _, _ = prepare(
        client, publisher, tmp_path, payload, ok=fault != "failed_process"
    )
    assert not [event for event in events if event.type == "story.prepared"]
    assert [event for event in events if event.type == "worker.failed"]


def test_ambiguity_is_reported_instead_of_inventing_questions(
    client, publisher, tmp_path
):
    events, _, _ = prepare(
        client,
        publisher,
        tmp_path,
        {"error": "The source does not define a free room."},
    )
    assert not [event for event in events if event.type == "story.prepared"]
    errors = [event for event in events if event.type == "error.raised"]
    assert len(errors) == 1
    assert errors[0].payload["kind"] == "blocked"


def test_claude_receives_the_original_questions_in_a_separate_session(
    client, publisher, tmp_path, monkeypatch
):
    prepared = deepcopy(EXAMPLES["story.prepared"])
    sessions = []
    author = FakeRunner(
        lambda *_: TurnResult(
            ok=True, text=json.dumps(prepared), session_ref="codex-session"
        )
    )
    author.capabilities = RunnerCaps(supports_resume=True, structured_preparations=True)

    def self_review(prompt, session_ref):
        sessions.append(session_ref)
        assert prepared["properties"][0]["question"] in prompt
        request = next(
            event
            for _, event in reversed(list(read_all(client, "testswarm")))
            if event.type == "story.validation.requested"
        )
        publisher.send(
            "analyst",
            "coordinator",
            "story.validation.judged",
            EXAMPLES["story.validation.judged"],
            in_reply_to=request.event_id,
            story_id="I1.S1",
            iteration_id="I1",
        )
        return TurnResult(ok=True, session_ref="claude-session")

    claude = FakeRunner(self_review)
    worker = ChainWorker(
        "testswarm",
        "analyst",
        claude,
        Path("roles/analyst.md"),
        tmp_path,
        tmp_path / "state",
        client,
        runners={
            "story.preparation.requested": author,
            "story.validation.requested": claude,
        },
    )
    monkeypatch.setattr(
        worker,
        "_handle_in_pinned_worktree",
        lambda env: worker._run_turn_loop(env, tmp_path),
    )
    for kind in ["story.preparation.requested", "story.validation.requested"]:
        publisher.send(
            "coordinator",
            "analyst",
            kind,
            EXAMPLES[kind],
            story_id="I1.S1",
            iteration_id="I1",
        )
        worker.handle(list(read_all(client, "testswarm"))[-1][1])
    assert sessions == [None]
    assert len(author.turns) == len(claude.turns) == 1
    assert (
        len(
            [
                event
                for _, event in read_all(client, "testswarm")
                if event.type == "story.validation.judged"
            ]
        )
        == 1
    )
