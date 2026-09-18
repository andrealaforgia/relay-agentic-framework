"""A worker can check a message without publishing it.

Replay of the I3.INT placeholder. The builder could not find the valid shape
of an iteration-level completion: it tried story_id "I3", then "I3.INT", then
"", then left it out, and each attempt was refused with no hint of the answer,
which is null. Having no way to validate without sending, it published a
completion whose summary was "test" to find out, and the live ledger recorded
a success claim that meant nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from relay.bus.keys import ledger_key
from relay.cli import send

BUILT = {"behaviour_id": "I3.INT", "iteration_id": "I3",
         "commit_sha": "fd4a80602bc94f08c7e7e0fcc74d7f70f7ba2beb", "attempt": 5,
         "summary": "no code change this attempt"}
ROLES = Path(__file__).parent.parent.parent / "roles"


def _run(monkeypatch, client, *extra: str, payload: dict[str, object]) -> int:
    monkeypatch.setattr(send, "get_client", lambda: client)
    monkeypatch.setattr(sys, "argv", [
        "relay-send", "--swarm", "testswarm", "--from", "builder", "--to", "coordinator",
        "--type", "behaviour.built", "--payload", json.dumps(payload), *extra])
    return send.main()


def test_check_validates_and_publishes_nothing(monkeypatch, client, capsys) -> None:
    rc = _run(monkeypatch, client, "--check", payload={**BUILT, "story_id": None})
    assert rc == 0
    assert "valid" in capsys.readouterr().out
    assert client.xrange(ledger_key("testswarm")) == []


def test_check_refuses_an_invalid_message_and_publishes_nothing(
        monkeypatch, client, capsys) -> None:
    rc = _run(monkeypatch, client, "--check", payload={**BUILT, "story_id": "I3.INT"})
    assert rc == 1
    assert "story_id" in capsys.readouterr().err
    assert client.xrange(ledger_key("testswarm")) == []


@pytest.mark.parametrize("story_id", ["I3", "I3.INT", ""])
def test_the_refusal_says_an_iteration_level_behaviour_has_no_story(
        monkeypatch, client, capsys, story_id: str) -> None:
    rc = _run(monkeypatch, client, payload={**BUILT, "story_id": story_id})
    assert rc == 1
    assert '"story_id": null' in capsys.readouterr().err


def test_the_refusal_names_null_when_story_id_is_left_out(monkeypatch, client, capsys) -> None:
    rc = _run(monkeypatch, client, payload=BUILT)
    assert rc == 1
    assert '"story_id": null' in capsys.readouterr().err


def test_builder_guidance_shows_an_iteration_level_completion() -> None:
    text = (ROLES / "builder.md").read_text()
    assert '"story_id": null' in text
    assert "--check" in text


def test_builder_guidance_forbids_probing_the_ledger() -> None:
    text = (ROLES / "builder.md").read_text().lower()
    assert "never publish a placeholder" in text
