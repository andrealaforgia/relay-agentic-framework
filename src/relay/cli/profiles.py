"""Per-role Claude permission profiles.

Written by `relay init` into <project>/.relay/settings/<role>.json and picked
up by the runner automatically. Never --dangerously-skip-permissions: in
headless mode a denied tool is refused silently, so these allowlists ARE the
role's capability surface — the builder can edit and run, the interpreter can
only read, delegate, and speak through relay-send.
"""

from __future__ import annotations

import json
from pathlib import Path

_RELAY_BASH = [
    "Bash(relay-send:*)",
    "Bash(relay-id:*)",
    "Bash(relay-id)",
]
_GIT_READ = ["Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)", "Bash(git status:*)"]

PROFILES: dict[str, dict[str, list[str]]] = {
    # conversational roles: read + subagents + relay-send; no edits, no arbitrary shell
    "interpreter": {
        "allow": ["Read", "Glob", "Grep", "TodoWrite",
                  "Bash(relay-inbox:*)", *_RELAY_BASH, *_GIT_READ],
        "deny": ["WebFetch", "WebSearch"],
    },
    "analyst": {
        # writes problem-analysis.md / user-stories.md / codebase-brief.md
        "allow": ["Read", "Glob", "Grep", "TodoWrite", "Task", "Write", "Edit",
                  *_RELAY_BASH, *_GIT_READ],
        "deny": ["WebFetch", "WebSearch"],
    },
    # writing roles: full workspace + shell (tests, git) — still inside the
    # project cwd and still auditable, unlike skip-permissions
    "specifier": {"allow": ["Read", "Glob", "Grep", "TodoWrite", "Task", "Write", "Edit", "Bash"],
                  "deny": ["WebFetch", "WebSearch"]},
    "builder": {"allow": ["Read", "Glob", "Grep", "TodoWrite", "Task", "Write", "Edit", "Bash"],
                "deny": ["WebFetch", "WebSearch"]},
    # gate roles (Phase 2): read-only + relay-send
    "reviewer": {"allow": ["Read", "Glob", "Grep", "Task", *_RELAY_BASH, *_GIT_READ],
                 "deny": ["Write", "Edit", "WebFetch", "WebSearch"]},
    "qa": {"allow": ["Read", "Glob", "Grep", "Task", "Bash"],
           "deny": ["Write", "Edit", "WebFetch", "WebSearch"]},
    "security": {"allow": ["Read", "Glob", "Grep", "Task", "Bash"],
                 "deny": ["Write", "Edit", "WebFetch", "WebSearch"]},
}


def settings_dir(project: Path) -> Path:
    return project / ".relay" / "settings"


def settings_path(project: Path, role: str) -> Path:
    return settings_dir(project) / f"{role}.json"


def write_profiles(project: Path, swarm: str) -> list[Path]:
    directory = settings_dir(project)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for role, perms in PROFILES.items():
        settings: dict[str, object] = {"permissions": perms}
        if role == "interpreter":
            # the Interpreter is a native Claude Code session; these hooks are
            # how relay mail reaches it without any terminal trickery:
            # - Stop: pending mail blocks the stop and feeds in as instruction
            # - UserPromptSubmit: the owner's words go on the ledger, queued
            #   mail rides in as extra context
            settings["hooks"] = {
                "Stop": [{"hooks": [{
                    "type": "command",
                    "command": f"relay-inbox --swarm {swarm} --hook-stop",
                }]}],
                "UserPromptSubmit": [{"hooks": [{
                    "type": "command",
                    "command": f"relay-inbox --swarm {swarm} --hook-prompt",
                }]}],
            }
        path = settings_path(project, role)
        path.write_text(json.dumps(settings, indent=2) + "\n")
        written.append(path)
    return written
