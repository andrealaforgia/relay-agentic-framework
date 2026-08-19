from relay.cli.watch import _event_row, events_view


def test_event_row_renders_all_columns(publisher, client) -> None:
    publisher.send("builder", "coordinator", "behaviour.built",
                   {"behaviour_id": "I1.S1.B1", "story_id": "I1.S1", "iteration_id": "I1",
                    "commit_sha": "a" * 40, "attempt": 2, "summary": "pieces now fall"},
                   behaviour_id="I1.S1.B1")
    from relay.bus.keys import ledger_key
    from relay.contract.envelope import Envelope
    ((_sid, fields),) = client.xrange(ledger_key("testswarm"))
    row = str(_event_row(Envelope.from_fields(fields)))
    assert "builder" in row and "coordinator" in row
    assert "behaviour.built" in row
    assert "I1.S1.B1" in row
    assert "attempt=2" in row and "summary=pieces now fall" in row


def test_events_view_prints_history_and_skips_foreign(publisher, client, capsys, monkeypatch) -> None:
    import relay.cli.watch as watch_mod

    monkeypatch.setenv("COLUMNS", "170")
    publisher.send("owner", "interpreter", "problem.stated", {"text": "a sand game"})
    client.xadd(watch_mod.ledger_key("testswarm"), {"from": "v1", "body": "old"})  # foreign
    publisher.send("interpreter", "analyst", "analysis.requested", {"problem": "a sand game"})
    monkeypatch.setattr(watch_mod, "get_client", lambda: client)
    events_view("testswarm", follow=False)
    out = capsys.readouterr().out
    assert "problem.stated" in out
    assert "analysis.requested" in out
    assert "another writer" in out
    assert out.index("problem.stated") < out.index("analysis.requested")  # ledger order


def test_system_events_get_their_own_lane(publisher, client, capsys, monkeypatch) -> None:
    import relay.cli.watch as watch_mod

    monkeypatch.setenv("COLUMNS", "170")
    publisher.send("owner", "interpreter", "problem.stated", {"text": "a sand game"})
    publisher.send(
        "builder", "system", "usage.reported",
        {"role": "builder", "model": "claude-sonnet-5", "trigger_type": "build.requested",
         "fresh_session": True, "session_turn": 1, "cost_usd": 0.42, "agent_turns": 3,
         "duration_s": 9.0, "input_tokens": 10, "cache_creation_input_tokens": 10,
         "cache_read_input_tokens": 10, "output_tokens": 10},
    )
    monkeypatch.setattr(watch_mod, "get_client", lambda: client)
    events_view("testswarm", follow=False)
    lines = capsys.readouterr().out.splitlines()

    work = next(line for line in lines if "problem.stated" in line)
    system = next(line for line in lines if "usage.reported" in line)
    # work stays left of the divider; system-addressed events sit right of it
    assert work.index("problem.stated") < work.index("┃")
    assert system.index("┃") < system.index("usage.reported")
    # and the lane mirrors the left format: origin → recipient  type  detail
    right = system[system.index("┃"):]
    assert right.index("builder") < right.index("→") < right.index("system") \
        < right.index("usage.reported") < right.index("cost_usd")
    assert any("→ system" in line for line in lines)  # the lane is labeled
