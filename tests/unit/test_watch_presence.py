"""The watch presence table must render every historical presence shape."""

from __future__ import annotations

import json
import time

from relay.bus.keys import presence_key
from relay.cli.watch import _presence


def test_presence_renders_json_bare_pid_and_garbage(client) -> None:
    client.set(presence_key("testswarm", "builder", "host"), json.dumps(
        {"pid": 1, "status": "working: build.requested", "since": time.time() - 30}
    ))
    client.set(presence_key("testswarm", "coordinator", "host"), "73685")  # old bare-pid shape
    client.set(presence_key("testswarm", "toolgate", "host"), "not json at all")

    table = _presence(client, "testswarm")  # must not raise
    rendered = "\n".join(str(col) for row in table.columns for col in row.cells)
    assert "working: build.requested" in rendered
    assert rendered.count("alive") == 2  # bare pid and garbage both degrade gracefully
