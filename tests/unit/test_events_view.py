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
