"""The watch board must fit the terminal (a Live view cannot scroll) and a
BLOCKED row must say why — 'blocked' with no reason is a question the human
then has to go dig for."""

from __future__ import annotations

from rich.console import Console

from relay.cli.watch import _board
from relay.coordinator.model import (
    Behaviour,
    BehaviourState,
    DecisionInfo,
    SwarmState,
)


def _state(n_stories: int, per_story: int) -> SwarmState:
    state = SwarmState()
    for s in range(1, n_stories + 1):
        for b in range(1, per_story + 1):
            bid = f"I1.S{s}.B{b}"
            state.behaviours[bid] = Behaviour(
                id=bid, iteration_id="I1", story_id=f"I1.S{s}", kind="ac",
                ac_text=f"criterion {bid}", title=f"outcome {bid}",
            )
            state.behaviour_order.append(bid)
    return state


def _render(table) -> str:
    console = Console(width=160, no_color=True)
    with console.capture() as capture:
        console.print(table)
    return capture.get()


def test_short_board_is_untouched() -> None:
    state = _state(2, 3)
    assert _board(state, max_rows=20).row_count == 6


def test_long_board_collapses_done_stories_and_overflow() -> None:
    state = _state(6, 5)  # 30 rows
    for bid, b in state.behaviours.items():
        if bid.startswith(("I1.S1.", "I1.S2.")):
            b.state = BehaviourState.DONE
    state.behaviours["I1.S3.B1"].state = BehaviourState.BUILD_DISPATCHED

    table = _board(state, max_rows=10)
    assert table.row_count <= 10
    rendered = _render(table)
    assert "all 5 behaviours done" in rendered      # S1/S2 are one line each
    assert "I1.S3.B1" in rendered                    # active work always visible
    assert "more not shown" in rendered              # the cut is announced


def test_a_finished_iteration_is_one_line_not_one_per_story() -> None:
    state = _state(3, 4)  # I1: S1..S3 × 4
    state.behaviours["I1.INT"] = Behaviour(
        id="I1.INT", iteration_id="I1", story_id=None, kind="integration",
        ac_text="the increment works end to end", state=BehaviourState.DONE,
    )
    state.behaviour_order.append("I1.INT")
    for b in state.behaviours.values():
        b.state = BehaviourState.DONE
    # a second, still-active iteration keeps the board over budget
    for n in (1, 2, 3, 4, 5, 6, 7, 8):
        bid = f"I2.S1.B{n}"
        state.behaviours[bid] = Behaviour(
            id=bid, iteration_id="I2", story_id="I2.S1", kind="ac",
            ac_text=f"criterion {bid}",
        )
        state.behaviour_order.append(bid)

    rendered = _render(_board(state, max_rows=12))
    assert "all 13 behaviours done" in rendered      # I1 collapsed whole
    assert "I1.S1 " not in rendered                  # not one line per story
    assert "I1.INT" not in rendered                  # the INT is inside the ✓


def test_relay_status_lists_the_full_board(client, publisher, tmp_path, monkeypatch) -> None:
    """The watch board's '… more not shown' row points at `relay status`,
    so `relay status` must genuinely print every behaviour, uncropped."""
    from typer.testing import CliRunner

    import relay.cli.main as main_mod
    from test_coordinator import ROADMAP

    monkeypatch.setenv("RELAY_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(main_mod, "get_client", lambda: client)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})

    result = CliRunner().invoke(main_mod.app, ["status", "--swarm", "testswarm"])
    assert result.exit_code == 0, result.output
    from relay.coordinator.projection import project as project_events
    from relay.ledger.reader import read_all

    state = project_events(env for _sid, env in read_all(client, "testswarm"))
    assert state.behaviour_order  # the fixture really has a board
    for bid in state.behaviour_order:
        assert bid in result.output


def test_blocked_row_carries_the_reason() -> None:
    state = _state(1, 2)
    state.behaviours["I1.S1.B1"].state = BehaviourState.BLOCKED
    state.decisions["gate-1"] = DecisionInfo(
        gate_id="gate-1", subject_id="I1.S1.B1", since="2026-08-19T10:00:00+00:00",
        last_ask="2026-08-19T10:00:00+00:00",
        reason="3 build attempts failed: the AT asserts a schema the plan never agreed",
    )
    rendered = _render(_board(state))
    assert "✗ 3 build attempts failed" in rendered

    # no open decision: fall back to the last recorded failure
    state.decisions.clear()
    state.behaviours["I1.S1.B1"].last_fail_reason = "mutation gate: 4 surviving mutants"
    rendered = _render(_board(state))
    assert "✗ mutation gate: 4 surviving mutants" in rendered
