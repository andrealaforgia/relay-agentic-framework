"""Claude Code as a runner: headless `claude -p` with STREAMING output.

Streaming is what makes a worker observable: every text block and tool call
the model produces is surfaced through `on_event` as it happens — the worker
writes it to its log and to its presence status, so `relay watch` and
`relay tail` show live activity instead of a minutes-long black box.
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


def _summarize_tool(name: str, tool_input: dict[str, Any]) -> str:
    if name == "Bash":
        return f"$ {str(tool_input.get('command', ''))[:160]}"
    if name in ("Write", "Edit", "Read"):
        return f"{name} {tool_input.get('file_path', '')}"
    if name == "Task":
        return f"Task → {tool_input.get('subagent_type', tool_input.get('description', ''))}"
    return f"{name} {json.dumps(tool_input)[:120]}"


def parse_stream_line(line: str) -> tuple[str | None, dict[str, Any] | None]:
    """('activity', ...) events for on_event; ('result', obj) at turn end."""
    line = line.strip()
    if not line:
        return None, None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None, None
    kind = obj.get("type")
    if kind == "assistant":
        blocks = (obj.get("message") or {}).get("content") or []
        parts = []
        for block in blocks:
            if block.get("type") == "text" and block.get("text", "").strip():
                parts.append(f"“{block['text'].strip()[:160]}”")
            elif block.get("type") == "tool_use":
                parts.append(_summarize_tool(block.get("name", "?"), block.get("input") or {}))
        if parts:
            return " | ".join(parts), None
    elif kind == "result":
        return None, obj
    return None, None


@dataclass
class ClaudeRunner:
    model: str | None = None
    settings_path: Path | None = None
    # Andrea's call: assistants run with full autonomy — in headless mode a
    # denied tool is a silent stall, and allowlists can never anticipate every
    # command a builder legitimately needs. Set skip_permissions=false per
    # role in relay.toml to fall back to the permission profile.
    skip_permissions: bool = True
    binary: str = "claude"
    capabilities: RunnerCaps = RunnerCaps(supports_resume=True)

    def build_command(self, prompt: str, session_ref: str | None) -> list[str]:
        cmd = [self.binary, "-p", prompt, "--output-format", "stream-json", "--verbose"]
        if self.skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if self.model:
            cmd += ["--model", self.model]
        if self.settings_path:
            cmd += ["--settings", str(self.settings_path)]
        if session_ref:
            cmd += ["--resume", session_ref]
        return cmd

    def run_turn(
        self,
        *,
        prompt: str,
        cwd: Path,
        session_ref: str | None,
        timeout_s: int,
        on_event: OnEvent | None = None,
    ) -> TurnResult:
        cmd = self.build_command(prompt, session_ref)

        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        timer = threading.Timer(timeout_s, proc.kill)
        timer.start()
        result_obj: dict[str, Any] | None = None
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                activity, result = parse_stream_line(line)
                if activity and on_event:
                    on_event(activity)
                if result is not None:
                    result_obj = result
            proc.wait()
        finally:
            timer.cancel()

        if proc.returncode != 0 and result_obj is None:
            stderr = proc.stderr.read() if proc.stderr else ""
            detail = stderr.strip()[-500:] or f"exit {proc.returncode}"
            if proc.returncode in (-9, 137):
                detail = f"claude killed after {timeout_s}s timeout"
            return TurnResult(ok=False, error=detail, session_ref=session_ref)
        if result_obj is None:
            return TurnResult(ok=False, error="claude stream ended without a result event",
                              session_ref=session_ref)
        return TurnResult(
            ok=not result_obj.get("is_error", False),
            text=str(result_obj.get("result", "")),
            session_ref=str(result_obj.get("session_id") or "") or session_ref,
            cost_usd=result_obj.get("total_cost_usd"),
            error=str(result_obj.get("result", ""))[:500] if result_obj.get("is_error") else None,
        )
