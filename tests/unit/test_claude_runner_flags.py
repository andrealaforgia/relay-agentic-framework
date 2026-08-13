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
