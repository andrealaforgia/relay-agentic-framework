"""`relay watch --tokens`: the bill, as it happens.

Same fold as `relay costs`, rendered live. What a live view has to add over
the report is rate and recency — what the swarm is spending right now, and
which turn just spent it — because the question being asked mid-run is "is
this getting away from me?", not "what did it come to".
"""

from rich.console import Console

from relay.cli.watch import _tokens_summary, _tokens_table, _usage_line, tokens_view
from relay.ledger.usage import UsageFold

USAGE = {"input_tokens": 20, "cache_creation_input_tokens": 40_000,
         "cache_read_input_tokens": 200_000, "output_tokens": 5_000}


def _usage(publisher, role, behaviour, cost, fresh=True, model="claude-sonnet-5"):
    publisher.send(
        role, "system", "usage.reported",
        {"role": role, "model": model, "trigger_type": "gate.requested",
         "fresh_session": fresh, "session_turn": 1, "cost_usd": cost,
         "agent_turns": 12, "duration_s": 30.0, **USAGE},
        behaviour_id=behaviour, iteration_id="I1",
    )


def _render(renderable) -> str:
    console = Console(width=160, no_color=True)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def test_the_table_ranks_roles_by_spend(client, publisher) -> None:
    _usage(publisher, "builder", "I1.S1.B1", 1.20)
    _usage(publisher, "reviewer", "I1.S1.B1", 0.30)
    fold = UsageFold()
    for _sid, env in _events(client):
        fold.add(env)

    rendered = _render(_tokens_table(fold.report()))
    assert rendered.index("builder") < rendered.index("reviewer")   # priciest first
    assert "$1.20" in rendered and "$0.30" in rendered
    assert "$1.50" in rendered                                       # total


def test_the_summary_reports_rate_warmth_and_cold_starts(client, publisher) -> None:
    _usage(publisher, "builder", "I1.S1.B1", 1.20, fresh=True)
    _usage(publisher, "builder", "I1.S1.B1", 0.60, fresh=False)
    fold = UsageFold()
    for _sid, env in _events(client):
        fold.add(env)

    rendered = _render(_tokens_summary(fold.report(), elapsed_s=1800.0))
    assert "$1.80" in rendered
    assert "$3.60/h" in rendered          # 1.80 in half an hour
    assert "83%" in rendered              # cache warmth: 400k read vs 80k written
    assert "cold starts 1 of 2 turns" in rendered


def test_the_rate_is_absent_before_it_means_anything(client, publisher) -> None:
    _usage(publisher, "builder", "I1.S1.B1", 1.20)
    fold = UsageFold()
    for _sid, env in _events(client):
        fold.add(env)
    rendered = _render(_tokens_summary(fold.report(), elapsed_s=2.0))
    assert "/h" not in rendered           # two seconds of evidence is not a rate


def test_each_turn_shows_who_spent_what_on_which_work_item(client, publisher) -> None:
    _usage(publisher, "qa", "I1.S1.B2", 0.44, model="claude-opus-5")
    (_sid, env), = list(_events(client))
    line = str(_usage_line(env))
    assert "qa" in line and "$0.44" in line
    assert "I1.S1.B2" in line
    assert "opus" in line                 # the tier that billed, at a glance
    assert "cold" in line                 # a fresh session is worth seeing


def test_the_view_follows_the_ledger_and_stops_when_asked(client, publisher, monkeypatch) -> None:
    import relay.cli.watch as watch_mod

    _usage(publisher, "builder", "I1.S1.B1", 1.20)
    monkeypatch.setattr(watch_mod, "get_client", lambda: client)
    tokens_view("testswarm", refresh_s=0.0, cycles=2)     # must not raise, must return


def test_an_empty_ledger_renders_without_spend(client, publisher, monkeypatch) -> None:
    import relay.cli.watch as watch_mod

    publisher.send("owner", "interpreter", "problem.stated", {"text": "noughts and crosses"})
    monkeypatch.setattr(watch_mod, "get_client", lambda: client)
    tokens_view("testswarm", refresh_s=0.0, cycles=1)
    assert "$0.00" in _render(_tokens_table(UsageFold().report()))


def _events(client):
    from relay.ledger.reader import read_all

    return [(sid, env) for sid, env in read_all(client, "testswarm")
            if env.type == "usage.reported"]
