"""Gate policy: which gates block at which granularity, and the loop limits.

Loaded from policies/gates.yaml (project override wins over the shipped
default). Phase 1 ships with all optional gates disabled — the specifier's
acceptance judgement is always mandatory and is not a policy entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class GateSpec:
    gate: str          # code_review | test_design | mutation | security
    role: str          # reviewer | qa | security
    timeout_s: int = 1800
    retries: int = 1


@dataclass(frozen=True)
class Policy:
    per_behaviour: tuple[GateSpec, ...] = ()
    per_story: tuple[GateSpec, ...] = ()
    per_iteration: tuple[GateSpec, ...] = ()
    wip_limit: int = 1
    max_attempts: int = 3

    @staticmethod
    def load(path: Path) -> "Policy":
        raw = yaml.safe_load(path.read_text()) or {}

        def gates(key: str) -> tuple[GateSpec, ...]:
            return tuple(
                GateSpec(
                    gate=entry["gate"],
                    role=entry["role"],
                    timeout_s=int(entry.get("timeout_s", 1800)),
                    retries=int(entry.get("retries", 1)),
                )
                for entry in raw.get(key) or []
            )

        return Policy(
            per_behaviour=gates("per_behaviour"),
            per_story=gates("per_story"),
            per_iteration=gates("per_iteration"),
            wip_limit=int(raw.get("wip_limit", 1)),
            max_attempts=int(raw.get("max_attempts", 3)),
        )
