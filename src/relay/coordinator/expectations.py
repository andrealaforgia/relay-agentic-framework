"""Story preparation is a protocol transition, not a reminder to a model."""

from __future__ import annotations

import json
import re
import hashlib

from relay import receipts
from typing import TYPE_CHECKING, Any

from relay.contract.envelope import Envelope
from relay.coordinator.model import SwarmState, Story, Behaviour, BehaviourState

if TYPE_CHECKING:
    from relay.coordinator.dispatcher import Dispatcher


def reject(state: SwarmState, env: Envelope, reason: str) -> None:
    state.evidence_rejections[env.event_id] = reason
    state.unescalated_errors[env.event_id] = f"{env.type}: {reason}"


def apply(state: SwarmState, env: Envelope) -> None:
    if env.type in (
        "problem.stated",
        "answers.given",
        "instruction.given",
        "feedback.given",
    ):
        state.sources[env.event_id] = str(
            env.payload.get("text") or json.dumps(env.payload, ensure_ascii=False)
        )
    if env.type == "roadmap.committed" and not state.sources:
        state.sources[env.event_id] = json.dumps(
            env.payload["roadmap"], ensure_ascii=False
        )
    if (
        env.type in ("run.requested", "run.completed")
        and env.payload.get("kind") == "evidence"
    ):
        apply_run(state, env)
        return
    if env.type == "decision.made":
        story = state.stories.get(str(env.payload.get("subject_id")))
        decision = state.decisions.get(str(env.payload.get("gate_id")))
        if story and story.evidence.protocol == 2 and decision and decision.closed:
            if env.payload["decision"] in ("retry", "fix"):
                story.evidence.failure = ""
                story.evidence.dispatches.clear()
                story.evidence.verification_attempts = 0
                story.evidence.invalidate()
                story.done_announced = False
            elif env.payload["decision"] == "drop":
                story.evidence.waived = True
                story.evidence.accepted_commit = ""
                story.done_announced = False
        return
    if env.type == "story.completed":
        story = state.stories.get(str(env.payload.get("story_id")))
        if (
            story
            and story.evidence.protocol == 2
            and (not story.evidence.accepted_commit or story.evidence.waived)
        ):
            story.done_announced = False
            reject(
                state, env, "a story cannot complete without verified property answers"
            )
        return
    if not env.type.startswith("story."):
        return
    story = state.stories.get(str(env.payload.get("story_id")))
    if story is None or story.evidence.protocol != 2:
        return
    evidence = story.evidence
    if env.payload.get("version") != evidence.version:
        reject(state, env, "superseded story version")
        return
    if env.type.endswith(".requested"):
        if env.event_id in evidence.seen_dispatches:
            return
        evidence.dispatches[env.type] = evidence.dispatches.get(env.type, 0) + 1
        evidence.seen_dispatches.add(env.event_id)
        evidence.pending_type = env.type
        evidence.pending_id = env.event_id
        evidence.pending_since = env.ts
        return
    expected = {
        "story.prepared": "story.preparation.requested",
        "story.verification.written": "story.verification.requested",
        "story.verification.reviewed": "story.verification.review.requested",
        "story.validation.judged": "story.validation.requested",
    }.get(env.type)
    if evidence.pending_type != expected or env.in_reply_to != evidence.pending_id:
        reject(state, env, "reply does not answer the current story dispatch")
        return
    if env.type == "story.prepared":
        obligations = {
            b.id: b.ac_text
            for b in state.story_behaviours(story.id)
            if b.kind != "integration"
        }
        expectations = env.payload["expectations"]
        properties = env.payload["properties"]
        valid = (
            len(expectations) == len(obligations)
            and {e["id"]: e["text"] for e in expectations} == obligations
        )
        valid = valid and all(
            ref["event_id"] in state.sources
            and ref["quote"] in state.sources[ref["event_id"]]
            for e in expectations
            for ref in e["source_refs"]
        )
        property_ids = [p["id"] for p in properties]
        valid = (
            valid
            and len(set(property_ids)) == len(property_ids)
            and all(
                p["id"].startswith(story.id + ".P")
                and set(p["expectation_ids"]) <= obligations.keys()
                for p in properties
            )
        )
        if not valid:
            reject(
                state,
                env,
                "expectations must preserve the roadmap and cite existing source text; properties must refer to this story",
            )
            return
        evidence.expectations = expectations
        evidence.properties = properties
    elif env.type == "story.verification.written":
        checks = env.payload["checks"]
        if set(checks) != {p["id"] for p in evidence.properties}:
            reject(state, env, "every property needs executable check selectors")
            return
        evidence.checks = checks
        evidence.verification_commit = env.payload["commit_sha"]
    elif env.type == "story.verification.reviewed":
        evidence.reviewed = env.payload["verdict"] == "pass"
        if not evidence.reviewed:
            evidence.feedback = env.payload["reason"]
            evidence.checks.clear()
    elif env.type == "story.validation.judged":
        if not apply_answers(state, story, env):
            return
    else:
        return
    evidence.pending_id = ""
    evidence.pending_type = ""


def advance_preparation(dispatcher: Dispatcher, state: SwarmState, story: Story) -> int:
    evidence = story.evidence
    if evidence.protocol != 2 or evidence.ready or story.escalated:
        return 0
    if evidence.pending_id:
        return 0
    if not evidence.expectations:
        return request(
            dispatcher,
            state,
            story,
            "analyst",
            "story.preparation.requested",
            {
                "expectations": [
                    {"id": b.id, "text": b.ac_text}
                    for b in state.story_behaviours(story.id)
                    if b.kind != "integration"
                ],
                "sources": [
                    {"event_id": key, "text": value}
                    for key, value in state.sources.items()
                ],
            },
        )
    if evidence.checks and not evidence.reviewed:
        return request(
            dispatcher,
            state,
            story,
            "qa",
            "story.verification.review.requested",
            {
                "expectations": evidence.expectations,
                "properties": evidence.properties,
                "checks": evidence.checks,
                "commit_sha": evidence.verification_commit,
                "reason": "Challenge the property checks for relevance, boundaries and tautologies",
            },
        )
    return request(
        dispatcher,
        state,
        story,
        "specifier",
        "story.verification.requested",
        {
            "expectations": evidence.expectations,
            "properties": evidence.properties,
            "reason": "Write executable property checks before implementation. "
            + evidence.feedback,
        },
    )


def request(
    dispatcher: Dispatcher,
    state: SwarmState,
    story: Story,
    role: str,
    type_: str,
    payload: dict[str, Any],
) -> int:
    evidence = story.evidence
    if evidence.dispatches.get(type_, 0) >= dispatcher._policy.max_attempts:
        story.escalated = True
        return dispatcher._ask_owner(
            state,
            story.id,
            f"{type_} did not complete within the attempt limit",
            story_id=story.id,
            iteration_id=story.iteration_id,
        )
    result = dispatcher._publisher.send(
        "coordinator",
        role,
        type_,
        {"story_id": story.id, "version": evidence.version, **payload},
        story_id=story.id,
        iteration_id=story.iteration_id,
    )
    # Do not advance the global replay cursor past events still waiting to be read.
    apply(state, result.envelope)
    return 1


def apply_run(state: SwarmState, env: Envelope) -> None:
    story = state.stories.get(str(env.story_id))
    if story is None or story.evidence.protocol != 2:
        return
    evidence = story.evidence
    if env.type == "run.requested":
        if env.event_id in evidence.seen_dispatches:
            return
        evidence.seen_dispatches.add(env.event_id)
        evidence.verification_attempts += 1
        declaration = env.payload.get("evidence", {})
        if declaration.get("version") != evidence.version:
            reject(state, env, "evidence request has a superseded story version")
            return
        evidence.run_id = env.payload["run_id"]
        evidence.candidate = env.payload["commit_sha"]
        evidence.declaration = declaration
        evidence.pending_id = env.event_id
        evidence.pending_type = "run.requested"
        evidence.pending_since = env.ts
        return
    run = state.runs.get(str(env.payload["run_id"]))
    if env.payload["run_id"] != evidence.run_id or run is None or run.exit_code is None:
        return
    receipt = env.payload.get("receipt", {})
    if evidence.receipt and evidence.receipt == receipt and not run.contested:
        return
    valid = (
        bool(receipt)
        and receipt.get("request_id") == evidence.pending_id
        and receipt.get("swarm") == env.swarm
        and receipt.get("run_id") == evidence.run_id
        and receipt.get("story_id") == story.id
        and receipt.get("version") == evidence.version
        and receipt.get("commit_sha") == evidence.candidate
        and receipt.get("declaration_digest") == receipts.digest(evidence.declaration)
        and receipt.get("exit_code") == env.payload["exit_code"]
        and hashlib.sha256(
            (str(receipt.get("stdout", "")) + str(receipt.get("stderr", ""))).encode()
        ).hexdigest()
        == env.payload["output_digest"]
        and not run.fault
        and not run.contested
    )
    if not valid:
        evidence.accepted_commit = ""
        evidence.answers.clear()
        story.done_announced = False
        evidence.failure = (
            "Execution evidence is missing or inconsistent: "
            + str(env.payload.get("summary", ""))
        )
        evidence.failure_owner = "environment"
        reject(state, env, evidence.failure)
    else:
        evidence.receipt = receipt
        try:
            receipts.coverage(
                evidence.declaration["bindings"], receipt.get("checks", [])
            )
            if receipt["exit_code"] != 0:
                raise ValueError("verification command failed")
        except (ValueError, KeyError, TypeError) as error:
            evidence.failure = (
                str(error) + "\n" + str(receipt.get("stdout", ""))[-1500:]
            )
            evidence.failure_owner = (
                "builder"
                if any(
                    check.get("outcome") == "failed"
                    for check in receipt.get("checks", [])
                )
                else "specifier"
            )
    evidence.pending_id = ""
    evidence.pending_type = ""


def apply_answers(state: SwarmState, story: Story, env: Envelope) -> bool:
    evidence = story.evidence
    answers = env.payload["answers"]
    expected = {p["id"] for p in evidence.properties}
    if (
        env.payload["run_id"] != evidence.run_id
        or env.payload["commit_sha"] != evidence.candidate
        or not evidence.receipt
        or len(answers) != len(expected)
        or {a["property_id"] for a in answers} != expected
        or any(a["run_id"] != evidence.run_id for a in answers)
    ):
        reject(
            state,
            env,
            "every original property needs an answer citing the current receipt",
        )
        return False
    evidence.answers = answers
    negative = [a for a in answers if a["verdict"] != "supported"]
    if negative:
        evidence.failure = "\n".join(
            a["property_id"] + ": " + a["explanation"] for a in negative
        )
        evidence.failure_owner = (
            "specifier"
            if any(a["verdict"] == "insufficient_evidence" for a in negative)
            else "builder"
        )
    else:
        evidence.accepted_commit = evidence.candidate
    return True


def bindings(state: SwarmState, story: Story) -> dict[str, list[str]]:
    return {
        **{b.id: b.test_paths for b in verification_behaviours(state, story)},
        **story.evidence.checks,
    }


def advance_validation(
    dispatcher: Dispatcher, state: SwarmState, story: Story, candidate: str
) -> tuple[bool, int]:
    from relay.coordinator.dispatcher import _new_run_id

    evidence = story.evidence
    if story.gates_waived or evidence.waived:
        evidence.waived = True
        return False, 0
    same_obligations = evidence.declaration == proof_declaration(state, story)
    if evidence.accepted_commit == candidate and same_obligations:
        return True, 0
    if evidence.accepted_commit and (
        evidence.accepted_commit != candidate or not same_obligations
    ):
        evidence.invalidate()
        evidence.verification_attempts = 0
        evidence.dispatches.pop("story.validation.requested", None)
        story.done_announced = False
    if evidence.failure:
        reason, owner = evidence.failure, evidence.failure_owner
        if (
            owner == "environment"
            or evidence.verification_attempts >= dispatcher._policy.max_attempts
        ):
            story.escalated = True
            return False, dispatcher._ask_owner(
                state,
                story.id,
                reason,
                story_id=story.id,
                iteration_id=story.iteration_id,
            )
        evidence.failure = ""
        evidence.invalidate()
        story.done_announced = False
        if owner == "specifier":
            evidence.checks.clear()
            evidence.reviewed = False
            evidence.feedback = reason
            return False, advance_preparation(dispatcher, state, story)
        return False, dispatcher._reopen_with_findings(
            state,
            story.int_behaviour_id,
            [
                {
                    "title": "Story expectations or properties are not fulfilled",
                    "detail": reason,
                    "severity": "major",
                    "source": "coordinator",
                }
            ],
        )
    if not evidence.ready or evidence.pending_id:
        return False, 0
    if not re.fullmatch(r"[0-9a-f]{40}", candidate):
        evidence.failure = (
            "Cannot dispatch story verification: the recorded candidate commit is missing or invalid. "
            "Reconcile the story's recorded build/evidence request and retry; required checks remain pending."
        )
        evidence.failure_owner = "environment"
        story.escalated = True
        return False, dispatcher._ask_owner(
            state, story.id, evidence.failure, story_id=story.id, iteration_id=story.iteration_id,
        )
    if not evidence.run_id:
        if evidence.verification_attempts >= dispatcher._policy.max_attempts:
            story.escalated = True
            return False, dispatcher._ask_owner(
                state,
                story.id,
                "Evidence runs exhausted their attempt limit",
                story_id=story.id,
                iteration_id=story.iteration_id,
            )
        declaration = proof_declaration(state, story)
        run_id = _new_run_id()
        result = dispatcher._publisher.send(
            "coordinator",
            "toolgate",
            "run.requested",
            {
                "run_id": run_id,
                "kind": "evidence",
                "commit_sha": candidate,
                "evidence": declaration,
                **dispatcher._command(state, "evidence", story.iteration_id),
            },
            story_id=story.id,
            iteration_id=story.iteration_id,
            commit_sha=candidate,
        )
        apply(state, result.envelope)
        return False, 1
    if evidence.receipt:
        return False, request(
            dispatcher,
            state,
            story,
            "analyst",
            "story.validation.requested",
            {
                "commit_sha": candidate,
                "run_id": evidence.run_id,
                "expectations": evidence.expectations,
                "properties": evidence.properties,
                "receipt": evidence.receipt,
            },
        )
    return False, 0


def supervise(dispatcher: Dispatcher, state: SwarmState, now_s: float) -> int:
    published = 0
    for story in state.stories.values():
        evidence = story.evidence
        if evidence.protocol != 2 or story.escalated or not evidence.pending_id:
            continue
        if not dispatcher._overdue(
            evidence.pending_since, now_s, dispatcher._policy.dispatch_timeout_s
        ):
            continue
        was_run = evidence.pending_type == "run.requested"
        evidence.pending_id = ""
        evidence.pending_type = ""
        if was_run:
            evidence.run_id = ""
        if not evidence.ready:
            published += advance_preparation(dispatcher, state, story)
        else:
            _, count = advance_validation(dispatcher, state, story, evidence.candidate)
            published += count
    return published


def test_baselines(state: SwarmState, story: Story) -> dict[str, str]:
    baselines = {
        selector.split("::", 1)[0]: story.evidence.verification_commit
        for selectors in story.evidence.checks.values()
        for selector in selectors
    }
    for behaviour in verification_behaviours(state, story):
        for selector in behaviour.test_paths:
            baselines[selector.split("::", 1)[0]] = behaviour.spec_commit or ""
    return baselines


def verification_behaviours(state: SwarmState, story: Story) -> list[Behaviour]:
    behaviours = state.story_behaviours(story.id)
    integration = state.behaviours[
        state.iterations[story.iteration_id].int_behaviour_id
    ]
    if integration.state == BehaviourState.DONE:
        behaviours.append(integration)
    return behaviours


def proof_declaration(state: SwarmState, story: Story) -> dict[str, Any]:
    evidence = story.evidence
    return {
        "version": evidence.version,
        "expectations": evidence.expectations,
        "properties": evidence.properties,
        "bindings": bindings(state, story),
        "verification_commit": evidence.verification_commit,
        "test_baselines": test_baselines(state, story),
    }
