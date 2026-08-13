"""The scripted runner that lets the whole relay run — and misbehave — with
zero model calls. Tests hand it a `respond(prompt, session_ref)` callable
that plays the model: publishing through the real relay-send path, publishing
nothing, publishing garbage, crashing, or double-publishing.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from relay.runners.base import RunnerCaps, TurnResult

Respond = Callable[[str, str | None], TurnResult | str | None]


class FakeRunner:
    capabilities = RunnerCaps(supports_resume=True)

    def __init__(self, respond: Respond) -> None:
        self._respond = respond
        self.turns: list[str] = []

    def run_turn(
        self,
        *,
        prompt: str,
        cwd: Path,
        session_ref: str | None,
        timeout_s: int,
    ) -> TurnResult:
        self.turns.append(prompt)
        outcome = self._respond(prompt, session_ref)
        if outcome is None:
            return TurnResult(ok=True, text="", session_ref=session_ref or "fake-session")
        if isinstance(outcome, str):
            return TurnResult(ok=True, text=outcome, session_ref=session_ref or "fake-session")
        return outcome


def crash_runner() -> FakeRunner:
    def _crash(prompt: str, session: str | None) -> TurnResult:
        raise RuntimeError("scripted runner crash")

    return FakeRunner(_crash)


def silent_runner() -> FakeRunner:
    """Claims success but publishes nothing — the exact failure mode
    verify-don't-trust exists for."""
    return FakeRunner(lambda _p, s: TurnResult(ok=True, text="done!", session_ref=s or "fake"))
