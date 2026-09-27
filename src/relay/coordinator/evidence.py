"""Versioned story obligations and their projected verification state."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StoryEvidence:
    protocol: int = 1
    version: int = 1
    expectations: list[dict[str, Any]] = field(default_factory=list)
    properties: list[dict[str, Any]] = field(default_factory=list)
    checks: dict[str, list[str]] = field(default_factory=dict)
    verification_commit: str = ""
    reviewed: bool = False
    pending_type: str = ""
    pending_id: str = ""
    pending_since: str = ""
    dispatches: dict[str, int] = field(default_factory=dict)
    seen_dispatches: set[str] = field(default_factory=set)
    feedback: str = ""
    candidate: str = ""
    run_id: str = ""
    declaration: dict[str, Any] = field(default_factory=dict)
    receipt: dict[str, Any] = field(default_factory=dict)
    answers: list[dict[str, Any]] = field(default_factory=list)
    accepted_commit: str = ""
    failure: str = ""
    failure_owner: str = "builder"
    verification_attempts: int = 0
    waived: bool = False

    @property
    def ready(self) -> bool:
        return bool(
            self.expectations and self.properties and self.checks and self.reviewed
        )

    def invalidate(self) -> None:
        self.run_id = ""
        self.candidate = ""
        self.receipt.clear()
        self.answers.clear()
        self.accepted_commit = ""
        self.pending_id = ""
        self.pending_type = ""
