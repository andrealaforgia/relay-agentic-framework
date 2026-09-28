"""Production supervises immediately after dispatch, before consuming its own event."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

from relay.bus import groups
from relay.coordinator.main import Coordinator
from relay.coordinator.model import Behaviour, Iteration, Story, SwarmState
from relay.coordinator import expectations
from relay.coordinator.projection import apply
from relay.ledger.reader import read_all

CANDIDATE = "c" * 40


def ready_state():
    state = SwarmState()
    state.roadmap_committed = True
    story = Story("I1.S1", "I1", "Example")
    state.stories[story.id] = story
    state.iterations["I1"] = Iteration(
        "I1", "Example", "Example", int_behaviour_id="I1.INT"
    )
    state.behaviours["I1.INT"] = Behaviour(
        "I1.INT", "I1", None, "integration", "Example"
    )
    evidence = story.evidence
    evidence.protocol = 2
    evidence.expectations = [{"id": "I1.S1.B1", "text": "Example"}]
    evidence.properties = [{"id": "I1.S1.P1"}]
    evidence.checks = {"I1.S1.P1": ["tests/test_example.py::test_example"]}
    evidence.verification_commit = "b" * 40
    evidence.reviewed = True
    evidence.pending_since = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    return state, story


def events(client, kind):
    return [event for _, event in read_all(client, "testswarm") if event.type == kind]


def test_coordinator_step_does_not_timeout_a_just_published_run(
    client, publisher, tmp_path, monkeypatch
):
    coordinator = Coordinator("testswarm", tmp_path, client=client)
    coordinator.state, story = ready_state()
    groups.ensure_group(client, coordinator.stream, coordinator.group)
    publisher.send(
        "owner", "interpreter", "problem.stated", {"text": "Wake the coordinator"}
    )
    monkeypatch.setattr(
        coordinator.dispatcher,
        "react",
        lambda state: expectations.advance_validation(
            coordinator.dispatcher, state, story, CANDIDATE
        )[1],
    )
    coordinator.step(block_ms=1)
    requested = events(client, "run.requested")
    assert len(requested) == 1
    evidence = story.evidence
    assert evidence.candidate == CANDIDATE
    assert evidence.run_id == requested[0].payload["run_id"]
    assert evidence.pending_since == requested[0].ts
    assert evidence.verification_attempts == 1
    coordinator.step(block_ms=1)
    assert evidence.verification_attempts == 1
    assert len(events(client, "run.requested")) == 1


def test_true_timeout_retries_same_candidate_and_echo_does_not_restore_old_request(
    client, tmp_path
):
    coordinator = Coordinator("testswarm", tmp_path, client=client)
    state, story = ready_state()
    replay, replay_story = deepcopy((state, story))
    dispatcher = coordinator.dispatcher
    expectations.advance_validation(dispatcher, state, story, CANDIDATE)
    first = events(client, "run.requested")[0]
    deadline = (
        datetime.fromisoformat(first.ts).timestamp()
        + dispatcher._policy.dispatch_timeout_s
        + 1
    )
    expectations.supervise(dispatcher, state, deadline)
    first, second = events(client, "run.requested")
    assert second.payload["commit_sha"] == CANDIDATE
    assert second.payload["run_id"] != first.payload["run_id"]
    assert story.evidence.verification_attempts == 2
    for event in [first, second]:
        apply(state, event)
        apply(replay, event)
        assert story.evidence.pending_id == second.event_id
    assert story.evidence == replay_story.evidence


def test_story_request_echo_does_not_restore_a_superseded_dispatch(client, tmp_path):
    coordinator = Coordinator("testswarm", tmp_path, client=client)
    state, story = ready_state()
    payload = {
        "expectations": [{"id": "I1.S1.B1", "text": "Example"}],
        "sources": [{"event_id": "01J5AB3CDEF4GH5JK6MN7PQ8RS", "text": "Example"}],
    }
    for _ in range(2):
        expectations.request(
            coordinator.dispatcher,
            state,
            story,
            "analyst",
            "story.preparation.requested",
            payload,
        )
    first, second = events(client, "story.preparation.requested")
    assert story.evidence.pending_since == second.ts
    apply(state, first)
    assert story.evidence.pending_id == second.event_id
    apply(state, second)
    assert story.evidence.dispatches["story.preparation.requested"] == 2


def test_missing_candidate_is_an_owner_decision_not_a_crash(client, tmp_path):
    coordinator = Coordinator("testswarm", tmp_path, client=client)
    state, story = ready_state()
    expectations.advance_validation(coordinator.dispatcher, state, story, "")
    assert not events(client, "run.requested")
    assert events(client, "decision.requested")
    assert story.escalated


def test_real_timeouts_exhaust_attempts_without_substituting_head(client, tmp_path):
    coordinator = Coordinator("testswarm", tmp_path, client=client)
    state, story = ready_state()
    dispatcher = coordinator.dispatcher
    expectations.advance_validation(dispatcher, state, story, CANDIDATE)
    for _ in range(dispatcher._policy.max_attempts):
        deadline = (
            datetime.fromisoformat(story.evidence.pending_since).timestamp()
            + dispatcher._policy.dispatch_timeout_s
            + 1
        )
        expectations.supervise(dispatcher, state, deadline)
    assert len(events(client, "run.requested")) == dispatcher._policy.max_attempts
    assert {
        event.payload["commit_sha"] for event in events(client, "run.requested")
    } == {CANDIDATE}
    assert story.escalated


def test_late_result_does_not_satisfy_a_replacement_run(client, publisher, tmp_path):
    coordinator = Coordinator("testswarm", tmp_path, client=client)
    state, story = ready_state()
    dispatcher = coordinator.dispatcher
    expectations.advance_validation(dispatcher, state, story, CANDIDATE)
    first = events(client, "run.requested")[0]
    expectations.supervise(
        dispatcher,
        state,
        datetime.fromisoformat(first.ts).timestamp()
        + dispatcher._policy.dispatch_timeout_s
        + 1,
    )
    second = events(client, "run.requested")[1]
    for event in [first, second]:
        apply(state, event)
    publisher.send(
        "toolgate",
        "coordinator",
        "run.completed",
        {
            "run_id": first.payload["run_id"],
            "kind": "evidence",
            "commit_sha": CANDIDATE,
            "exit_code": 0,
            "duration_s": 1,
            "output_digest": "a" * 64,
        },
        in_reply_to=first.event_id,
        story_id=story.id,
        iteration_id=story.iteration_id,
    )
    apply(state, events(client, "run.completed")[0])
    assert story.evidence.pending_id == second.event_id
    assert not story.evidence.receipt
    assert not story.evidence.accepted_commit
