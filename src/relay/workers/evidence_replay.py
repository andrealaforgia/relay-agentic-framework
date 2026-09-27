"""Repeat a recorded verification recipe without dispatching model work."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from ulid import ULID

from relay.contract.envelope import Envelope
from relay.workers.evidence_runner import execute


@dataclass
class LocalExecution:
    project: Path
    swarm: str
    commands: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    result: dict[str, Any] = field(default_factory=dict)

    def _complete(
        self,
        env: Envelope,
        exit_code: int,
        duration: float,
        output: str,
        fault: str | None = None,
        receipt: dict[str, Any] | None = None,
    ) -> str:
        self.result = {
            "exit_code": exit_code,
            "duration_s": duration,
            "output": output,
            "fault": fault,
            "receipt": receipt,
        }
        return str(env.payload["run_id"])


def replay(project: Path, request: Envelope) -> dict[str, Any]:
    if request.type != "run.requested" or request.payload.get("kind") != "evidence":
        raise ValueError("only an evidence execution recipe can be replayed")
    repeat = request.model_copy(
        update={
            "event_id": str(ULID()),
            "seq": None,
            "payload": {**request.payload, "run_id": "run-" + str(ULID())},
        }
    )
    target = LocalExecution(project=project, swarm=request.swarm)
    execute(target, repeat)
    return target.result
