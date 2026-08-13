"""Claude Code as a runner: headless `claude -p`, resumable sessions,
per-role permission profiles (never --dangerously-skip-permissions)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from relay.runners.base import RunnerCaps, TurnResult


@dataclass
class ClaudeRunner:
    model: str | None = None
    settings_path: Path | None = None
    binary: str = "claude"
    capabilities: RunnerCaps = RunnerCaps(supports_resume=True)

    def run_turn(
        self,
        *,
        prompt: str,
        cwd: Path,
        session_ref: str | None,
        timeout_s: int,
    ) -> TurnResult:
        cmd = [self.binary, "-p", prompt, "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]
        if self.settings_path:
            cmd += ["--settings", str(self.settings_path)]
        if session_ref:
            cmd += ["--resume", session_ref]
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout_s
            )
        except subprocess.TimeoutExpired:
            return TurnResult(ok=False, error=f"claude timed out after {timeout_s}s")
        if proc.returncode != 0:
            return TurnResult(ok=False, error=proc.stderr.strip() or f"exit {proc.returncode}",
                              session_ref=session_ref)
        try:
            reply = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return TurnResult(ok=False, error="claude emitted non-JSON output",
                              text=proc.stdout[-2000:], session_ref=session_ref)
        return TurnResult(
            ok=not reply.get("is_error", False),
            text=str(reply.get("result", "")),
            session_ref=str(reply.get("session_id") or "") or session_ref,
            cost_usd=reply.get("total_cost_usd"),
            error=str(reply.get("result", ""))[:500] if reply.get("is_error") else None,
        )
