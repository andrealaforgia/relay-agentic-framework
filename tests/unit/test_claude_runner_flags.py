from pathlib import Path

from relay.runners.claude import ClaudeRunner


def test_skip_permissions_is_the_default() -> None:
    cmd = ClaudeRunner(model="opus").build_command("do it", session_ref=None)
    assert "--dangerously-skip-permissions" in cmd
    assert "--resume" not in cmd


def test_profile_mode_opt_out() -> None:
    runner = ClaudeRunner(skip_permissions=False, settings_path=Path("/tmp/p.json"))
    cmd = runner.build_command("do it", session_ref="sess-1")
    assert "--dangerously-skip-permissions" not in cmd
    assert cmd[cmd.index("--settings") + 1] == "/tmp/p.json"
    assert cmd[cmd.index("--resume") + 1] == "sess-1"


def test_effort_and_budget_reach_the_command_line() -> None:
    """Effort caps the agentic loop; the budget caps the bill. Both are the
    framework's controls, not the model's."""
    cmd = ClaudeRunner(model="sonnet", effort="medium", max_budget_usd=1.5).build_command(
        "do it", session_ref=None
    )
    assert cmd[cmd.index("--effort") + 1] == "medium"
    assert cmd[cmd.index("--max-budget-usd") + 1] == "1.5"


def test_neither_flag_appears_when_unset() -> None:
    cmd = ClaudeRunner(model="sonnet").build_command("do it", session_ref=None)
    assert "--effort" not in cmd and "--max-budget-usd" not in cmd
