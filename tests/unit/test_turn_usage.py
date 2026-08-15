"""A turn's billable footprint must survive the runner boundary.

`claude -p --output-format stream-json` ends every turn with a result event
carrying `usage`, `modelUsage` (the tier that ACTUALLY billed — the check that
would have caught the whole-swarm-on-opus incident) and `num_turns` (how many
agentic loops the invocation spent, i.e. how much rediscovery it did).
"""

from relay.runners.claude import turn_from_result

RESULT = {
    "type": "result",
    "is_error": False,
    "result": "done",
    "session_id": "sess-9",
    "total_cost_usd": 0.42,
    "num_turns": 7,
    "usage": {
        "input_tokens": 12,
        "cache_creation_input_tokens": 16031,
        "cache_read_input_tokens": 17748,
        "output_tokens": 44,
    },
    "modelUsage": {"claude-sonnet-5": {"costUSD": 0.42}},
}


def test_turn_result_carries_the_billable_footprint() -> None:
    turn = turn_from_result(RESULT, fallback_session=None, configured_model="sonnet")
    assert turn.ok
    assert turn.session_ref == "sess-9"
    assert turn.cost_usd == 0.42
    assert turn.agent_turns == 7
    assert turn.usage == {
        "input_tokens": 12,
        "cache_creation_input_tokens": 16031,
        "cache_read_input_tokens": 17748,
        "output_tokens": 44,
    }


def test_model_is_the_one_that_billed_not_the_one_we_asked_for() -> None:
    turn = turn_from_result(RESULT, fallback_session=None, configured_model="sonnet")
    assert turn.model == "claude-sonnet-5"


def test_model_falls_back_to_the_configured_tier_when_unreported() -> None:
    turn = turn_from_result(
        {"type": "result", "result": "ok", "session_id": "s"},
        fallback_session=None,
        configured_model="sonnet",
    )
    assert turn.model == "sonnet"
    assert turn.usage == {}
    assert turn.cost_usd is None


def test_the_priciest_model_wins_when_a_turn_spans_several() -> None:
    turn = turn_from_result(
        {**RESULT, "modelUsage": {"claude-haiku-4-5": {"costUSD": 0.01},
                                  "claude-opus-5": {"costUSD": 3.20}}},
        fallback_session=None,
        configured_model="sonnet",
    )
    assert turn.model == "claude-opus-5"


BUDGET_EXHAUSTED = {
    "type": "result",
    "subtype": "error_max_budget_usd",
    "is_error": True,
    "terminal_reason": "budget_exhausted",
    "session_id": "sess-9",
    "total_cost_usd": 1.5,
    "num_turns": 22,
    "usage": {"cache_read_input_tokens": 900_000, "output_tokens": 3_000},
}


def test_a_turn_stopped_by_its_budget_says_so() -> None:
    """The result carries no text when the ceiling stops it, so the runner
    names the failure — and flags it, because retrying only spends again."""
    turn = turn_from_result(BUDGET_EXHAUSTED, fallback_session=None, configured_model="sonnet")
    assert not turn.ok
    assert turn.budget_exhausted
    assert "budget ceiling" in (turn.error or "")
    assert "22 loops" in (turn.error or "")
    assert turn.usage["cache_read_input_tokens"] == 900_000   # still billed, still recorded


def test_an_ordinary_error_is_not_a_budget_stop() -> None:
    turn = turn_from_result(
        {"type": "result", "is_error": True, "result": "tool failed", "session_id": "s"},
        fallback_session=None, configured_model="sonnet",
    )
    assert not turn.budget_exhausted and turn.error == "tool failed"
