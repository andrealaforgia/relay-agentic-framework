"""OpenAI Codex CLI as a runner: `codex exec --json`, resumable threads.

Most work is published via relay-send. For code review, the host validates
the final JSON response and publishes it without exposing Redis to Codex. Sandbox level maps from the
role's write needs (the analogue of the Claude permission profiles).

Codex event stream (JSONL): thread.started {thread_id}, item.completed
{item:{type: agent_message|command_execution|..., ...}}, turn.completed,
turn.failed {error}. Where a build of codex doesn't support resume, the
worker's prompts are self-contained (they carry the full trigger), so a lost
thread costs context, never correctness.
"""

from __future__ import annotations

import json
import os
import signal
import tempfile
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
    sandbox: str = "workspace-write"  # read-only | workspace-write
    model: str | None = None
    effort: str | None = None
    binary: str = "codex"
    capabilities: RunnerCaps = RunnerCaps(supports_resume=True, structured_reviews=True)

    def run_turn(
        self,
        *,
        prompt: str,
        cwd: Path,
        session_ref: str | None,
        timeout_s: int,
        on_event: OnEvent | None = None,
    ) -> TurnResult:
        cmd = [self.binary, "exec", "--sandbox", self.sandbox]
        if session_ref:
            cmd += ["resume", session_ref]
        cmd += ["--json", "--skip-git-repo-check"]
        if self.model:
            cmd += ["--model", self.model]
        if self.effort:
            cmd += ["-c", "model_reasoning_effort=" + json.dumps(self.effort)]
        cmd += [prompt]

        with tempfile.TemporaryFile() as errors:
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=errors,
                    text=True,
                    start_new_session=True,
                )
            except FileNotFoundError:
                return TurnResult(
                    ok=False,
                    error=f"{self.binary} not installed",
                    session_ref=session_ref,
                    model=self.model,
                )
            timer = threading.Timer(timeout_s, _kill_process_group, args=(proc,))
            timer.start()
            thread_id: str | None = session_ref
            terminal: str | None = None
            last_text = ""
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    activity, new_thread, term = parse_codex_line(line)
                    try:
                        event = json.loads(line)
                        item = event.get("item") or {}
                        if (
                            event.get("type") == "item.completed"
                            and item.get("type") == "agent_message"
                        ):
                            last_text = str(item.get("text", ""))
                    except (ValueError, AttributeError):
                        pass
                    if new_thread:
                        thread_id = new_thread
                    if activity:
                        if on_event:
                            on_event(activity)
                    if term is not None:
                        terminal = term
                proc.wait()
            finally:
                timer.cancel()

            if terminal == "ok" and proc.returncode == 0:
                return TurnResult(
                    ok=True, text=last_text, session_ref=thread_id, model=self.model
                )
            if terminal is not None:
                return TurnResult(
                    ok=False,
                    text=last_text,
                    error=terminal if terminal != "ok" else f"exit {proc.returncode}",
                    session_ref=thread_id,
                    model=self.model,
                )
            errors.seek(0, os.SEEK_END)
            errors.seek(max(0, errors.tell() - 400))
            stderr = errors.read().decode(errors="replace").strip()
            return TurnResult(
                ok=proc.returncode == 0,
                text=last_text,
                error=None
                if proc.returncode == 0
                else (stderr or f"exit {proc.returncode}"),
                session_ref=thread_id,
                model=self.model,
            )


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
