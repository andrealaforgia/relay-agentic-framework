"""Cost is a fold over the ledger, like everything else that is true (D3).

Session transcripts are the wrong home for burn data: they live outside the
swarm, they are attributed by guesswork, and `relay destroy` takes them with
it. Every model turn therefore publishes `usage.reported` against the work
item it was serving, so "what did this behaviour cost, on which model" is a
replay — and survives the swarm it measured.
"""

from pathlib import Path

from relay.ledger.reader import read_all
from relay.ledger.usage import billed_input_equivalents, fold_usage
from relay.runners.base import TurnResult
from relay.runners.fake import FakeRunner, silent_runner
from relay.workers.chain import ChainWorker

ROLES_DIR = Path(__file__).resolve().parents[2] / "roles"
SHA = "a" * 40

USAGE = {
    "input_tokens": 12,
    "cache_creation_input_tokens": 16031,
    "cache_read_input_tokens": 17748,
    "output_tokens": 44,
}


def _worker(client, runner, tmp_path, role="builder"):
    return ChainWorker(
        "testswarm", role, runner,
        playbook_path=ROLES_DIR / f"{role}.md",
        workspace=tmp_path, state_dir=tmp_path / role, client=client,
    )


def _replying_runner(publisher, role="builder"):
    def respond(prompt, session_ref):
        import re
        event_id = re.search(r"event_id: (\S+)", prompt).group(1)
        publisher.send(role, "coordinator", "error.raised",
                       {"kind": "other", "detail": "noted"}, in_reply_to=event_id)
        return TurnResult(ok=True, session_ref=session_ref or "sess-1",
                          cost_usd=0.42, model="claude-sonnet-5",
                          usage=USAGE, agent_turns=7)

    return FakeRunner(respond)


def _build_msg(publisher, bid="I1.S1.B1"):
    publisher.send("coordinator", "builder", "build.requested",
                   {"behaviour_id": bid, "spec_commit_sha": SHA, "test_paths": ["t.py"]},
                   behaviour_id=bid, iteration_id="I1", story_id="I1.S1")


def _usage_events(client):
    return [e for _sid, e in read_all(client, "testswarm") if e.type == "usage.reported"]


def test_a_turn_publishes_its_footprint_against_the_work_item(client, publisher, tmp_path) -> None:
    worker = _worker(client, _replying_runner(publisher), tmp_path)
    worker.start()
    _build_msg(publisher)
    worker.step(block_ms=1)

    events = _usage_events(client)
    assert len(events) == 1
    event = events[0]
    assert (event.from_role, event.to_role) == ("builder", "system")
    assert event.behaviour_id == "I1.S1.B1"
    assert event.iteration_id == "I1"
    assert event.payload["model"] == "claude-sonnet-5"
    assert event.payload["trigger_type"] == "build.requested"
    assert event.payload["cost_usd"] == 0.42
    assert event.payload["agent_turns"] == 7
    assert event.payload["cache_read_input_tokens"] == 17748
    assert event.payload["duration_s"] >= 0


def test_footprint_records_whether_the_session_was_cold(client, publisher, tmp_path) -> None:
    worker = _worker(client, _replying_runner(publisher), tmp_path)
    worker.start()
    _build_msg(publisher)
    worker.step(block_ms=1)
    _build_msg(publisher)          # same behaviour: the session is resumed
    worker.step(block_ms=1)

    fresh = [e.payload["fresh_session"] for e in _usage_events(client)]
    turns = [e.payload["session_turn"] for e in _usage_events(client)]
    assert fresh == [True, False]
    assert turns == [1, 2]


def test_usage_is_never_mistaken_for_the_models_reply(client, publisher, tmp_path) -> None:
    """The verify-don't-trust check reads the ledger. A worker's own
    bookkeeping must not be able to satisfy it — or a silent model would look
    like a productive one."""
    runner = silent_runner()
    worker = _worker(client, runner, tmp_path)
    worker.start()
    _build_msg(publisher)
    worker.step(block_ms=1)

    assert len(runner.turns) == 3          # corrected twice, never satisfied
    failures = [e for _s, e in read_all(client, "testswarm") if e.type == "worker.failed"]
    assert failures, "a model that never replies must fail loudly"


def test_cost_per_role_and_per_behaviour_is_a_fold(client, publisher) -> None:
    def usage(role, behaviour, cost, fresh):
        publisher.send(
            role, "system", "usage.reported",
            {"role": role, "model": "claude-sonnet-5", "trigger_type": "build.requested",
             "fresh_session": fresh, "session_turn": 1, "cost_usd": cost,
             "agent_turns": 3, "duration_s": 10.0, **USAGE},
            behaviour_id=behaviour, iteration_id="I1",
        )

    usage("builder", "I1.S1.B1", 0.40, True)
    usage("builder", "I1.S1.B2", 0.60, True)
    usage("reviewer", "I1.S1.B1", 0.25, False)

    report = fold_usage(client, "testswarm")
    assert report.by_role["builder"]["cost_usd"] == 1.0
    assert billed_input_equivalents(report.total) == (
        3 * 12 + 3 * 16031 * 1.25 + 3 * 17748 * 0.1
    )
    assert report.by_role["builder"]["turns"] == 2
    assert report.by_role["reviewer"]["cost_usd"] == 0.25
    assert report.by_behaviour["I1.S1.B1"]["cost_usd"] == 0.65
    assert report.by_model["claude-sonnet-5"]["turns"] == 3
    assert report.by_role["builder"]["fresh_sessions"] == 2
    assert report.by_role["reviewer"]["fresh_sessions"] == 0
    assert report.total["cost_usd"] == 1.25


def test_the_ledger_view_reports_spend_and_cache_warmth(client, publisher, monkeypatch, capsys) -> None:
    import relay.cli.main as cli

    publisher.send(
        "reviewer", "system", "usage.reported",
        {"role": "reviewer", "model": "claude-sonnet-5", "trigger_type": "gate.requested",
         "fresh_session": True, "session_turn": 1, "cost_usd": 0.31, "agent_turns": 9, **USAGE},
        behaviour_id="I1.S1.B1",
    )
    monkeypatch.setattr(cli, "get_client", lambda: client)
    cli._costs_from_ledger("testswarm", by_behaviour=False)
    out = capsys.readouterr().out
    assert "reviewer" in out and "$0.31" in out
    assert "claude-sonnet-5" in out
    assert "1 of 1 turns started cold" in out


def test_the_ledger_view_says_so_when_nothing_was_recorded(client, monkeypatch, capsys) -> None:
    import typer

    import relay.cli.main as cli

    monkeypatch.setattr(cli, "get_client", lambda: client)
    try:
        cli._costs_from_ledger("testswarm", by_behaviour=False)
    except typer.Exit as e:
        assert e.exit_code == 0
    assert "no model turns recorded" in capsys.readouterr().out


def test_a_turn_without_a_reported_cost_is_estimated_not_zeroed(tmp_path) -> None:
    """The Interpreter's transcript records tokens and no price. A zero there
    would understate the swarm's priciest role by its entire spend."""
    from relay.pricing import estimate_cost

    opus = estimate_cost("claude-opus-5", {"cache_read_input_tokens": 1_000_000,
                                           "cache_creation_input_tokens": 100_000,
                                           "output_tokens": 10_000})
    # 1M reads at 0.1x + 100k writes at 2x = 300k billed input @ $5/M = $1.50,
    # plus 10k output @ $25/M = $0.25
    assert round(opus, 2) == 1.75
    assert estimate_cost("claude-sonnet-5", {"output_tokens": 1_000_000}) == 15.0
    assert estimate_cost("something-unknown", {"output_tokens": 1_000_000}) == 15.0
    assert estimate_cost("claude-opus-5", {}) == 0.0
