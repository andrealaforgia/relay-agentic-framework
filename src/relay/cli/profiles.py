"""Per-role Claude permission profiles.

Written by `relay init` into <project>/.relay/settings/<role>.json and picked
up by the runner automatically. Never --dangerously-skip-permissions: in
headless mode A DENIED TOOL IS REFUSED SILENTLY — so these allowlists ARE the
role's capability surface, and a playbook instruction with no matching entry
here does not fail loudly, it simply never happens. The builder can edit and
run; the interpreter reads, delegates, speaks through relay-send, and commits
the Analyst's documents when the roadmap is approved.
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
# The Interpreter's ONE write to the repository: on roadmap approval it commits
# the Analyst's documents, which until then exist only as untracked files on one
# machine — invisible to every worker that reads the repo and absent from the PR.
# Narrow on purpose: add/commit/push and the two reads that decide whether a
# push is even possible. No branch, no reset, no rebase, no arbitrary Bash.
_GIT_COMMIT_ANALYSIS = [
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(git push:*)",
    "Bash(git remote:*)",
    "Bash(git rev-parse:*)",
]

PROFILES: dict[str, dict[str, list[str]]] = {
    # conversational roles: read + subagents + relay-send; no edits, no arbitrary shell
    "interpreter": {
        "allow": ["Read", "Glob", "Grep", "TodoWrite",
                  "Bash(relay-inbox:*)", *_RELAY_BASH, *_GIT_READ,
                  *_GIT_COMMIT_ANALYSIS],
        "deny": ["Write", "Edit", "WebFetch", "WebSearch"],
    },
    "analyst": {
        # writes problem-analysis.md / user-stories.md / codebase-brief.md
        "allow": ["Read", "Glob", "Grep", "TodoWrite", "Task", "Write", "Edit",
                  *_RELAY_BASH, *_GIT_READ],
        "deny": ["WebFetch", "WebSearch"],
    },
    "planner": {
        # writes docs/relay/plans/<iteration>.md and commits it on approval
        "allow": ["Read", "Glob", "Grep", "TodoWrite", "Task", "Write", "Edit",
                  *_RELAY_BASH, *_GIT_READ, *_GIT_COMMIT_ANALYSIS],
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
    from relay.cli.entrypoints import relay_command

    directory = settings_dir(project)
    directory.mkdir(parents=True, exist_ok=True)
    inbox = relay_command("relay-inbox")
    written = []
    for role, perms in PROFILES.items():
        settings: dict[str, object] = {"permissions": perms}
        if role == "interpreter":
            # the Interpreter is a native Claude Code session; these hooks are
            # how relay mail reaches it without any terminal trickery:
            # - Stop: pending mail blocks the stop and feeds in as instruction
            # - UserPromptSubmit: the owner's words go on the ledger, queued
            #   mail rides in as extra context
            # Absolute path on purpose: hooks run under `/bin/sh -c` with
            # whatever PATH the session inherited, which on a login shell does
            # not include ~/.local/bin.
            settings["hooks"] = {
                "Stop": [{"hooks": [{
                    "type": "command",
                    "command": f"{inbox} --swarm {swarm} --hook-stop",
                }]}],
                "UserPromptSubmit": [{"hooks": [{
                    "type": "command",
                    "command": f"{inbox} --swarm {swarm} --hook-prompt",
                }]}],
            }
        path = settings_path(project, role)
        path.write_text(json.dumps(settings, indent=2) + "\n")
        written.append(path)
    return written
