"""CodexRunner against a stub binary emitting codex-style JSONL events."""

from __future__ import annotations

import stat
from pathlib import Path

from relay.runners.codex import CodexRunner, parse_codex_line


def _stub(tmp_path: Path, body: str) -> str:
    script = tmp_path / "codex"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_successful_turn_captures_thread_and_activity(tmp_path: Path) -> None:
    stub = _stub(
        tmp_path,
        """
echo '{"type":"thread.started","thread_id":"th_123"}'
echo '{"type":"item.completed","item":{"type":"command_execution","command":"relay-send --swarm x ..."}}'
echo '{"type":"item.completed","item":{"type":"agent_message","text":"published the reply"}}'
echo '{"type":"turn.completed","usage":{}}'
""",
    )
    events: list[str] = []
    result = CodexRunner(binary=stub).run_turn(
        prompt="do the thing",
        cwd=tmp_path,
        session_ref=None,
        timeout_s=30,
        on_event=events.append,
    )
    assert result.ok
    assert result.session_ref == "th_123"
    assert any("relay-send" in e for e in events)


def test_failed_turn_surfaces_error(tmp_path: Path) -> None:
    stub = _stub(
        tmp_path,
        """
echo '{"type":"thread.started","thread_id":"th_9"}'
echo '{"type":"turn.failed","error":{"message":"sandbox denied"}}'
""",
    )
    result = CodexRunner(binary=stub).run_turn(
        prompt="x",
        cwd=tmp_path,
        session_ref=None,
        timeout_s=30,
    )
    assert not result.ok
    assert "sandbox denied" in (result.error or "")
    assert result.session_ref == "th_9"


def test_resume_passes_thread_id(tmp_path: Path) -> None:
    stub = _stub(
        tmp_path,
        """
echo "$@" > args.txt
echo '{"type":"turn.completed"}'
""",
    )
    result = CodexRunner(binary=stub).run_turn(
        prompt="continue",
        cwd=tmp_path,
        session_ref="th_123",
        timeout_s=30,
    )
    assert result.ok
    args = (tmp_path / "args.txt").read_text()
    assert "resume th_123" in args
    assert result.session_ref == "th_123"


def test_missing_binary_is_a_clean_error(tmp_path: Path) -> None:
    result = CodexRunner(binary=str(tmp_path / "nope")).run_turn(
        prompt="x",
        cwd=tmp_path,
        session_ref=None,
        timeout_s=5,
    )
    assert not result.ok and "not installed" in (result.error or "")


def test_parse_ignores_noise() -> None:
    assert parse_codex_line("") == (None, None, None)
    assert parse_codex_line("not json") == (None, None, None)
    assert parse_codex_line('{"type":"turn.started"}') == (None, None, None)


def test_review_text_is_not_truncated_and_nonzero_exit_cannot_pass(tmp_path):
    import json

    message = json.dumps({"verdict": "fail", "detail": "x" * 500})
    event = json.dumps(
        {"type": "item.completed", "item": {"type": "agent_message", "text": message}}
    )
    stub = _stub(
        tmp_path, "echo '" + event + '\'\necho \'{"type":"turn.completed"}\'\nexit 1\n'
    )
    result = CodexRunner(binary=stub).run_turn(
        prompt="review", cwd=tmp_path, session_ref=None, timeout_s=5
    )
    assert result.text == message
    assert not result.ok


def test_verbose_stderr_does_not_block_a_successful_review(tmp_path):
    import sys

    stub = _stub(
        tmp_path,
        f'''"{sys.executable}" -c 'import sys; sys.stderr.write("x" * 200000)'
echo '{{"type":"item.completed","item":{{"type":"agent_message","text":"complete review"}}}}'
echo '{{"type":"turn.completed"}}'
''',
    )
    result = CodexRunner(binary=stub).run_turn(
        prompt="review", cwd=tmp_path, session_ref=None, timeout_s=2
    )
    assert result.ok
    assert result.text == "complete review"
