"""relay-inbox: the native Claude Code session's mail tap."""

from __future__ import annotations

import io
import json

import pytest

from relay.bus.keys import ledger_key
from relay.cli import inbox
from relay.contract.envelope import Envelope


def _mail(publisher, text="Which calendar?") -> None:
    publisher.send("analyst", "interpreter", "questions.raised",
                   {"question_id": "q-01J5AB3CDEF4GH5JK6MN7PQ8RS", "questions": [text]})


def _run(client, argv: list[str], stdin: str = "", monkeypatch=None, capsys=None) -> str:
    monkeypatch.setattr("sys.argv", ["relay-inbox", *argv])
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    monkeypatch.setattr(inbox, "get_client", lambda: client)
    inbox.main()
    return capsys.readouterr().out


def test_drain_prints_and_acks(client, publisher, monkeypatch, capsys) -> None:
    _mail(publisher)
    out = _run(client, ["--swarm", "testswarm"], monkeypatch=monkeypatch, capsys=capsys)
    assert "questions.raised" in out and "relay-send" in out
    # drained: a second call finds nothing
    out = _run(client, ["--swarm", "testswarm"], monkeypatch=monkeypatch, capsys=capsys)
    assert "(no relay mail)" in out


def test_hook_stop_blocks_only_when_mail_pending(client, publisher, monkeypatch, capsys) -> None:
    out = _run(client, ["--swarm", "testswarm", "--hook-stop"],
               monkeypatch=monkeypatch, capsys=capsys)
    assert out.strip() == ""  # no mail -> no block, session may stop
    _mail(publisher)
    out = _run(client, ["--swarm", "testswarm", "--hook-stop"],
               monkeypatch=monkeypatch, capsys=capsys)
    decision = json.loads(out)
    assert decision["decision"] == "block"
    assert "questions.raised" in decision["reason"]


def test_hook_prompt_records_owner_words_problem_then_feedback(
    client, publisher, monkeypatch, capsys
) -> None:
    payload = json.dumps({"prompt": "I want a sand tetris game"})
    _run(client, ["--swarm", "testswarm", "--hook-prompt"], stdin=payload,
         monkeypatch=monkeypatch, capsys=capsys)
    payload = json.dumps({"prompt": "granules should be 2x2 pixels"})
    _run(client, ["--swarm", "testswarm", "--hook-prompt"], stdin=payload,
         monkeypatch=monkeypatch, capsys=capsys)
    types = [f["type"] for _s, f in client.xrange(ledger_key("testswarm"))]
    assert types == ["problem.stated", "feedback.given"]


def test_hook_prompt_surfaces_queued_mail_and_skips_slash_commands(
    client, publisher, monkeypatch, capsys
) -> None:
    _mail(publisher)
    out = _run(client, ["--swarm", "testswarm", "--hook-prompt"],
               stdin=json.dumps({"prompt": "/help"}), monkeypatch=monkeypatch, capsys=capsys)
    assert "questions.raised" in out  # mail surfaced as context
    _run(client, ["--swarm", "testswarm", "--hook-prompt"],
         stdin=json.dumps({"prompt": "<task-notification>noise</task-notification>"}),
         monkeypatch=monkeypatch, capsys=capsys)
    types = [f["type"] for _s, f in client.xrange(ledger_key("testswarm"))
             if f["from"] == "owner"]
    assert types == []  # slash commands are not owner utterances


def test_owner_addressed_progress_is_surfaced_but_interpreter_own_traffic_is_not(
    client, publisher, monkeypatch, capsys
) -> None:
    publisher.send("coordinator", "owner", "progress.reported",
                   {"iteration_id": "I1", "behaviours_done": 1, "behaviours_total": 3})
    publisher.send("interpreter", "analyst", "analysis.requested", {"problem": "p"})
    out = _run(client, ["--swarm", "testswarm"], monkeypatch=monkeypatch, capsys=capsys)
    assert "progress.reported" in out
    assert "analysis.requested" not in out  # its own outbound traffic is not mail
