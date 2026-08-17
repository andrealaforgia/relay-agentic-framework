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
    # How much work goes into ONE model transaction. `behaviour` is a
    # transaction per behaviour; `story` batches the story's behaviours into
    # one, so the codebase is explored once rather than once per slice. The
    # discipline is unchanged either way: one failing test per criterion, one
    # commit per behaviour, gates on the diff.
    spec_granularity: str = "behaviour"
    build_granularity: str = "behaviour"
    # Plan mode: no behaviour is dispatched for an iteration until a change
    # plan (docs/relay/plans/<iteration>.md, authored with the Owner in
    # `relay plan`) is committed. Fail closed, like every other gate.
    plan_required: bool = False

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
            spec_granularity=str(raw.get("spec_granularity", "behaviour")),
            build_granularity=str(raw.get("build_granularity", "behaviour")),
            plan_required=bool(raw.get("plan_required", False)),
        )
