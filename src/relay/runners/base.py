"""The runner protocol: 'invoke this brain with this prompt in this directory,
resumably'. The framework never parses model stdout for work product — the
model's only output channel is relay-send, and verification reads the ledger.
TurnResult.text exists for logs and viewers only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class RunnerCaps:
    supports_resume: bool


@dataclass(frozen=True)
class TurnResult:
    ok: bool
    text: str = ""
    session_ref: str | None = None
    cost_usd: float | None = None
    error: str | None = None


class Runner(Protocol):
    capabilities: RunnerCaps

    def run_turn(
        self,
        *,
        prompt: str,
        cwd: Path,
        session_ref: str | None,
        timeout_s: int,
    ) -> TurnResult: ...
