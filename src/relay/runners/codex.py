"""OpenAI Codex CLI as a runner: `codex exec --json`, resumable threads.

Same contract as every runner: the model's work product is what it publishes
via relay-send; stdout text is for logs only. Sandbox level maps from the
role's write needs (the analogue of the Claude permission profiles).

Codex event stream (JSONL): thread.started {thread_id}, item.completed
{item:{type: agent_message|command_execution|..., ...}}, turn.completed,
turn.failed {error}. Where a build of codex doesn't support resume, the
worker's prompts are self-contained (they carry the full trigger), so a lost
thread costs context, never correctness.
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relay.runners.base import RunnerCaps, TurnResult

OnEvent = Callable[[str], None]


def parse_codex_line(line: str) -> tuple[str | None, str | None, str | None]:
    """Returns (activity, thread_id, terminal) — terminal is 'ok'/'error: …'."""
    line = line.strip()
    if not line:
        return None, None, None
    try:
        obj: dict[str, Any] = json.loads(line)
    except json.JSONDecodeError:
        return None, None, None
    kind = obj.get("type")
    if kind == "thread.started":
        return None, str(obj.get("thread_id") or "") or None, None
    if kind == "item.completed":
        item = obj.get("item") or {}
        item_type = item.get("type")
        if item_type == "agent_message":
            text = str(item.get("text", "")).strip()
            return (f"“{text[:160]}”" if text else None), None, None
        if item_type == "command_execution":
            return f"$ {str(item.get('command', ''))[:160]}", None, None
        if item_type in ("file_change", "patch_apply"):
            return f"{item_type}: {str(item.get('path', item))[:120]}", None, None
        return None, None, None
    if kind == "turn.completed":
        return None, None, "ok"
    if kind in ("turn.failed", "error"):
        detail = obj.get("error") or obj.get("message") or kind
        return None, None, f"error: {json.dumps(detail)[:300]}"
    return None, None, None


@dataclass
class CodexRunner:
    sandbox: str = "workspace-write"   # read-only | workspace-write
    model: str | None = None
    binary: str = "codex"
    capabilities: RunnerCaps = RunnerCaps(supports_resume=True)

    def run_turn(
        self,
        *,
        prompt: str,
        cwd: Path,
        session_ref: str | None,
        timeout_s: int,
        on_event: OnEvent | None = None,
    ) -> TurnResult:
        cmd = [self.binary, "exec"]
        if session_ref:
            cmd += ["resume", session_ref]
        cmd += ["--json", "--sandbox", self.sandbox, "--skip-git-repo-check"]
        if self.model:
            cmd += ["--model", self.model]
        cmd += [prompt]

        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
        except FileNotFoundError:
            return TurnResult(ok=False, error=f"{self.binary} not installed",
                              session_ref=session_ref)
        timer = threading.Timer(timeout_s, proc.kill)
        timer.start()
        thread_id: str | None = session_ref
        terminal: str | None = None
        last_text = ""
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                activity, new_thread, term = parse_codex_line(line)
                if new_thread:
                    thread_id = new_thread
                if activity:
                    last_text = activity
                    if on_event:
                        on_event(activity)
                if term is not None:
                    terminal = term
            proc.wait()
        finally:
            timer.cancel()

        if terminal == "ok":
            return TurnResult(ok=True, text=last_text, session_ref=thread_id)
        if terminal is not None:
            return TurnResult(ok=False, error=terminal, session_ref=thread_id)
        stderr = (proc.stderr.read() if proc.stderr else "").strip()[-400:]
        return TurnResult(
            ok=proc.returncode == 0,
            text=last_text,
            error=None if proc.returncode == 0 else (stderr or f"exit {proc.returncode}"),
            session_ref=thread_id,
        )
