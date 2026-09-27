"""Human and machine inspection of the same story evidence projection."""

from __future__ import annotations

from typing import Any
from relay.contract.envelope import Envelope
from relay.coordinator.model import SwarmState


def story_report(state: SwarmState, story_id: str) -> dict[str, Any]:
    if story_id not in state.stories:
        raise ValueError(f"unknown story: {story_id}")
    story = state.stories[story_id]
    evidence = story.evidence
    status = "preparing"
    if evidence.protocol == 1:
        status = "legacy_unverified"
    elif evidence.waived or story.gates_waived:
        status = "risk_accepted_unverified"
    elif evidence.failure or story.escalated:
        status = "blocked"
    elif evidence.accepted_commit and story.done_announced:
        status = "verified"
    elif evidence.receipt:
        status = "awaiting_property_answers"
    elif evidence.ready:
        status = "awaiting_execution"
    answered = {
        a["property_id"] for a in evidence.answers if a["verdict"] == "supported"
    }
    return {
        "story_id": story.id,
        "title": story.title,
        "protocol_version": evidence.protocol,
        "version": evidence.version,
        "status": status,
        "candidate": evidence.candidate,
        "accepted_commit": evidence.accepted_commit,
        "run_id": evidence.run_id,
        "expectations": evidence.expectations,
        "properties": evidence.properties,
        "checks": evidence.checks,
        "receipt": evidence.receipt,
        "answers": evidence.answers,
        "missing_answers": [
            p["id"] for p in evidence.properties if p["id"] not in answered
        ],
        "failure": evidence.failure,
        "pending": evidence.pending_type,
        "attempts": evidence.verification_attempts,
    }


def story_metrics(events: list[Envelope], story_id: str) -> dict[str, Any]:
    from datetime import datetime
    from relay.ledger.usage import UsageFold

    selected = [
        env
        for env in events
        if env.story_id == story_id or str(env.payload.get("story_id", "")) == story_id
    ]
    usage = UsageFold()
    for env in selected:
        usage.add(env)
    total = usage.total
    elapsed = None
    if selected:
        elapsed = (
            datetime.fromisoformat(selected[-1].ts)
            - datetime.fromisoformat(selected[0].ts)
        ).total_seconds()
    return {
        "model_turns": total["turns"],
        "input_tokens": total["input_tokens"],
        "output_tokens": total["output_tokens"],
        "cache_read_input_tokens": total["cache_read_input_tokens"],
        "cache_creation_input_tokens": total["cache_creation_input_tokens"],
        "cost_usd": total["cost_usd"],
        "elapsed_s": elapsed,
        "verification_runs": sum(
            env.type == "run.requested" and env.payload.get("kind") == "evidence"
            for env in selected
        ),
        "scope": "recorded events for this story; shared iteration work is excluded",
    }
