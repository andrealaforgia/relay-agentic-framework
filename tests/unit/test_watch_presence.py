"""The watch presence table must render every historical presence shape —
and the complete roster: a worker that died is a red row, never an absent
one, and the native sessions (interpreter, curator, planner) are listed."""

from __future__ import annotations

import json
import time

from relay.bus.keys import presence_key
from relay.cli.watch import _presence


def _rendered(table) -> str:
    return "\n".join(str(col) for row in table.columns for col in row.cells)


def test_presence_renders_json_bare_pid_and_garbage(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RELAY_STATE_ROOT", str(tmp_path))
    client.set(presence_key("testswarm", "builder", "host"), json.dumps(
        {"pid": 1, "status": "working: build.requested", "since": time.time() - 30}
    ))
    client.set(presence_key("testswarm", "coordinator", "host"), "73685")  # old bare-pid shape
    client.set(presence_key("testswarm", "toolgate", "host"), "not json at all")

    table = _presence(client, "testswarm")  # must not raise
    rendered = _rendered(table)
    assert "working: build.requested" in rendered
    assert rendered.count("alive") == 2  # bare pid and garbage both degrade gracefully


def test_dead_worker_is_a_red_row_not_an_absent_one(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RELAY_STATE_ROOT", str(tmp_path))
    run = tmp_path / "testswarm" / "run"
    run.mkdir(parents=True)
    (run / "builder.pid").write_text("999999999")  # no such pid, no heartbeat

    rendered = _rendered(_presence(client, "testswarm"))
    assert "builder" in rendered
    assert "DOWN" in rendered


def test_native_sessions_are_on_the_roster(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RELAY_STATE_ROOT", str(tmp_path))
    rendered = _rendered(_presence(client, "testswarm"))
    # no session started yet: the interpreter row still exists and says how to open one
    assert "interpreter" in rendered and "relay chat" in rendered
    assert "curator" not in rendered  # only shown once a session exists

    for role in ("interpreter", "curator"):
        marker = tmp_path / "testswarm" / role / "native-session"
        marker.parent.mkdir(parents=True)
        marker.write_text("started")
    rendered = _rendered(_presence(client, "testswarm"))
    assert "curator" in rendered and "relay learn" in rendered
    assert "relay plan" not in rendered   # planning lives in chat now
