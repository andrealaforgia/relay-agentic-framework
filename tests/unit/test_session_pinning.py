"""`claude --continue` resumes the most recent conversation in the DIRECTORY,
so `relay plan` after `relay chat` reopened the chat session. Every native
session is pinned to its own UUID instead: started with --session-id,
resumed with --resume, stored in the role's marker file."""

from __future__ import annotations

import uuid
from pathlib import Path

from relay.cli.main import _session_pin_args


def test_first_launch_pins_a_fresh_session(tmp_path: Path) -> None:
    marker = tmp_path / "planner" / "native-session"
    args, fresh = _session_pin_args(marker, new=False)
    assert fresh
    assert args[0] == "--session-id"
    assert uuid.UUID(marker.read_text())  # the pin survives for next time
    assert args[1] == marker.read_text()


def test_relaunch_resumes_the_SAME_conversation(tmp_path: Path) -> None:
    marker = tmp_path / "planner" / "native-session"
    first, _ = _session_pin_args(marker, new=False)
    again, fresh = _session_pin_args(marker, new=False)
    assert not fresh
    assert again == ["--resume", first[1]]  # this role's session, no other


def test_two_roles_never_share_a_session(tmp_path: Path) -> None:
    chat, _ = _session_pin_args(tmp_path / "interpreter" / "native-session", new=False)
    plan, _ = _session_pin_args(tmp_path / "planner" / "native-session", new=False)
    assert chat[1] != plan[1]


def test_new_flag_abandons_the_pin(tmp_path: Path) -> None:
    marker = tmp_path / "curator" / "native-session"
    first, _ = _session_pin_args(marker, new=False)
    second, fresh = _session_pin_args(marker, new=True)
    assert fresh and second[0] == "--session-id" and second[1] != first[1]


def test_legacy_started_marker_starts_pinned(tmp_path: Path) -> None:
    marker = tmp_path / "interpreter" / "native-session"
    marker.parent.mkdir(parents=True)
    marker.write_text("started")  # pre-pinning marker: no uuid to resume
    args, fresh = _session_pin_args(marker, new=False)
    assert fresh and args[0] == "--session-id"
    assert uuid.UUID(marker.read_text())
