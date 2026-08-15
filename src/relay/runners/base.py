"""The runner protocol: 'invoke this brain with this prompt in this directory,
resumably'. The framework never parses model stdout for work product — the
model's only output channel is relay-send, and verification reads the ledger.
TurnResult.text exists for logs and viewers only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
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
    # what the turn cost to run. `model` is the tier that actually billed —
    # the check that would have caught a whole swarm silently running on the
    # priciest one. `agent_turns` is how many loops the invocation spent, i.e.
    # how much of the codebase it had to rediscover. Empty when the runner
    # reports nothing; never guessed.
    model: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    agent_turns: int | None = None


class Runner(Protocol):
    capabilities: RunnerCaps

    def run_turn(
        self,
        *,
        prompt: str,
        cwd: Path,
        session_ref: str | None,
        timeout_s: int,
        on_event: Callable[[str], None] | None = None,
    ) -> TurnResult: ...
