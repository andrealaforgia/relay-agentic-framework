"""relay costs — where the tokens actually went.

Claude Code records per-turn usage in every session transcript under
~/.claude/projects/<project-path>. This aggregates them per role: API calls,
fresh input, cache writes (billed at a premium), cache reads (the quadratic
tell — huge numbers here mean long sessions re-read on every turn), output.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

_ROLE_RE = re.compile(r"You are the '(\w+)' assistant|# (Interpreter|Analyst|Specifier|Builder|Reviewer|QA|Security|Sentinel)\\n")
USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens", "output_tokens")


def transcripts_dir(project: Path) -> Path:
    munged = str(project.resolve()).replace("/", "-").replace(".", "-")
    return Path.home() / ".claude" / "projects" / munged


def analyze(project: Path) -> tuple[dict[str, dict[str, int]], int]:
    """role -> usage sums (+ 'api_calls'); returns (per_role, session_count)."""
    per_role: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    root = transcripts_dir(project)
    sessions = 0
    for f in root.glob("*.jsonl") if root.is_dir() else []:
        role = "(other)"
        role_found = False
        counted = False
        try:
            with f.open() as fh:
                for line in fh:
                    if not role_found:
                        m = _ROLE_RE.search(line[:4000])
                        if m:
                            role = (m.group(1) or m.group(2)).lower()
                            role_found = True
                    if '"usage"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "assistant":
                        continue
                    usage = (obj.get("message") or {}).get("usage") or {}
                    per_role[role]["api_calls"] += 1
                    counted = True
                    for key in USAGE_KEYS:
                        per_role[role][key] += int(usage.get(key) or 0)
        except OSError:
            continue
        if counted:
            sessions += 1
    return dict(per_role), sessions
