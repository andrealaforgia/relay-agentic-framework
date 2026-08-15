"""What a role may say, and exactly what a valid payload looks like.

The protocol reminder used to name the message type and stop there, so a
worker that did not know the payload shape had to go and find out. On a live
run the analyst ran `find /` across the whole machine — twice — and ended up
reading the framework's own schema files from inside the project it was
supposed to be working on.

Nothing about that needed a model or a search. The worker loads the contract
in process; it knows every type the role may publish and the exact shape of
each. So we put it in the prompt: required fields, plus the example payload
the contract already carries for every type (test-enforced, so it cannot rot).

Bounded, because this rides in every prompt the role ever sends.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from relay.contract.loader import REPO_ROOT, Contract

BUDGET = 4_000
EXAMPLES_PATH = REPO_ROOT / "contract" / "examples.yaml"
# system types every role may publish; the worker sends these itself
_HOUSEKEEPING = {"worker.started", "worker.stopped", "worker.failed", "session.started",
                 "message.quarantined", "gap.detected", "contract.upgraded", "usage.reported"}


def _examples() -> dict[str, Any]:
    try:
        return dict(yaml.safe_load(EXAMPLES_PATH.read_text()) or {})
    except (OSError, yaml.YAMLError):
        return {}


def outgoing(contract: Contract, role: str) -> dict[str, list[str]]:
    """recipient -> the types this role may send them, housekeeping aside."""
    out: dict[str, list[str]] = {}
    for (frm, to), types in contract.edges.items():
        if frm != role:
            continue
        speakable = sorted(t for t in types if t not in _HOUSEKEEPING)
        if speakable:
            out[to] = speakable
    return out


def for_role(contract: Contract, role: str) -> str:
    """The role's whole vocabulary, with a valid payload for each type."""
    edges = outgoing(contract, role)
    if not edges:
        return ""
    examples = _examples()
    lines = ["", "== What you may publish (this is the whole list) =="]
    for to, types in sorted(edges.items()):
        for type_ in types:
            schema = contract.payload_schemas.get(type_, {})
            required = ", ".join(schema.get("required", [])) or "(no required fields)"
            lines.append(f"\n--to {to} --type {type_}   required: {required}")
            example = examples.get(type_)
            if example is not None:
                lines.append("  valid payload: " + json.dumps(example, sort_keys=True))
    lines.append(
        "\nThese payloads are validated before they reach the ledger. If one is "
        "rejected, fix it from the shapes above — never go looking for the "
        "framework's own files, and never read or write outside this project."
    )
    text = "\n".join(lines)
    return text if len(text) <= BUDGET else text[:BUDGET] + "\n  … (truncated)"


def required_fields(contract: Contract, type_: str) -> list[str]:
    """Used by relay-send to say what was missing rather than only that
    something was."""
    schema: dict[str, Any] = contract.payload_schemas.get(type_, {})
    return [str(f) for f in schema.get("required", [])]


def examples_path() -> Path:
    return EXAMPLES_PATH
