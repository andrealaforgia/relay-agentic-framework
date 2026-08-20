"""Permission profiles ARE the capability surface: in headless mode a denied
tool is refused silently, so a playbook instruction with no matching allow
entry does not fail loudly — it simply never happens."""

from __future__ import annotations

import json
from pathlib import Path

from relay.cli.profiles import PROFILES, settings_path, write_profiles


def test_the_interpreter_can_commit_the_analysis_it_is_told_to_commit() -> None:
    allow = PROFILES["interpreter"]["allow"]
    for command in ("Bash(git add:*)", "Bash(git commit:*)", "Bash(git push:*)"):
        assert command in allow
    # and the read that decides whether a push is even possible
    assert "Bash(git remote:*)" in allow


def test_the_interpreter_still_cannot_edit_the_codebase() -> None:
    """Committing the Analyst's documents is its one write to the repository.
    It must not become a role that changes code."""
    profile = PROFILES["interpreter"]
    assert "Write" not in profile["allow"] and "Edit" not in profile["allow"]
    assert "Write" in profile["deny"] and "Edit" in profile["deny"]
    assert "Bash" not in profile["allow"]          # never arbitrary shell
    assert not any(c.startswith("Bash(git reset") or c.startswith("Bash(git rebase")
                   for c in profile["allow"])


def test_profiles_land_where_the_runner_looks(tmp_path: Path) -> None:
    write_profiles(tmp_path, "acme")
    written = json.loads(settings_path(tmp_path, "interpreter").read_text())
    assert "Bash(git commit:*)" in written["permissions"]["allow"]
