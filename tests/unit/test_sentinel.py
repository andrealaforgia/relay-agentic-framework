"""Sentinel mechanical checks, control-plane worker duties, strikes, pause."""

from __future__ import annotations

from pathlib import Path

from relay.bus.keys import ledger_key
from relay.contract.envelope import Envelope
from relay.runners.fake import FakeRunner
from relay.workers.chain import ChainWorker
from relay.workers.sentinel import ACK_TIMEOUT_S, SentinelWorker

ROLES_DIR = Path(__file__).resolve().parents[2] / "roles"
SHA = "a" * 40


def _sentinel(client, tmp_path, respond=None) -> SentinelWorker:
    return SentinelWorker(
        "testswarm",
        runner=FakeRunner(respond or (lambda _p, _s: "clean")),
        playbook_path=ROLES_DIR / "sentinel.md",
        workspace=tmp_path,
        state_dir=tmp_path / "sentinel-state",
        client=client,
    )


def _events(client, type_: str) -> list[Envelope]:
    return [Envelope.from_fields(f) for _s, f in client.xrange(ledger_key("testswarm"))
            if f["type"] == type_]


def test_verdict_citing_unknown_run_is_corrected_deterministically(client, publisher, tmp_path) -> None:
    publisher.send("specifier", "coordinator", "work.acceptance_verdict",
                   {"behaviour_id": "I1.S1.B1", "verdict": "pass",
                    "run_id": "run-01J5AB3CDEF4GH5JK6MN7PQ8RS"})  # no such run.completed
    sentinel = _sentinel(client, tmp_path)
    sentinel.run_forever(block_ms=1, max_cycles=1)
    (correction,) = _events(client, "control.correction")
    assert correction.to_role == "specifier"
    assert correction.payload["rule_id"] == "evidence.run-not-on-ledger"
    assert correction.payload["required_remedy"] == "retract"

    # restart: the same violation is never corrected twice (dedup from ledger)
    second = _sentinel(client, tmp_path)
    second.run_forever(block_ms=1, max_cycles=1)
    assert len(_events(client, "control.correction")) == 1


def test_gate_verdict_for_unknown_gate_is_corrected(client, publisher, tmp_path) -> None:
    publisher.send("reviewer", "coordinator", "gate.verdict",
                   {"gate_id": "gate-01J5AB3CDEF4GH5JK6MN7PQ8RS", "verdict": "pass",
                    "findings": []})
    sentinel = _sentinel(client, tmp_path)
    sentinel.run_forever(block_ms=1, max_cycles=1)
    (correction,) = _events(client, "control.correction")
    assert correction.payload["rule_id"] == "gate.never-requested"


def test_worker_acks_corrections_and_feeds_them_to_next_turn(client, publisher, tmp_path) -> None:
    prompts: list[str] = []

    def respond(prompt: str, _s):
        prompts.append(prompt)
        # publish an on-contract reply so the turn verifies
        import re
        event_id = re.search(r"event_id: (\S+)", prompt).group(1)
        publisher.send("builder", "coordinator", "work.error",
                       {"kind": "other", "detail": "noted"}, in_reply_to=event_id)
        return "ok"

    builder = ChainWorker(
        "testswarm", "builder", FakeRunner(respond),
        playbook_path=ROLES_DIR / "builder.md",
        workspace=tmp_path, state_dir=tmp_path / "b", client=client,
    )
    builder.start()
    # sentinel corrects the builder
    publisher.send("sentinel", "builder", "control.correction",
                   {"finding_id": "find-01J5AB3CDEF4GH5JK6MN7PQ8RS",
                    "subject_event_id": "01J5AB3CDEF4GH5JK6MN7PQ8RS",
                    "rule_id": "realm.builder.no-domain-reinterpretation",
                    "required_remedy": "acknowledge_rule", "note": "stick to what now works"})
    builder.step(block_ms=1)
    (ack,) = _events(client, "control.ack")
    assert ack.from_role == "builder"  # acked by the WORKER, mechanically
    assert prompts == []               # no model turn was spent on the ack

    # the next real work turn carries the correction
    publisher.send("coordinator", "builder", "work.build_requested",
                   {"behaviour_id": "I1.S1.B1", "spec_commit_sha": SHA, "test_paths": ["t.py"]},
                   behaviour_id="I1.S1.B1")
    builder.step(block_ms=1)
    assert len(prompts) == 1
    assert "Sentinel corrections" in prompts[0]
    assert "realm.builder.no-domain-reinterpretation" in prompts[0]
    assert builder.pending_corrections == []  # drained


def test_pause_parks_work_and_resume_processes_it(client, publisher, tmp_path) -> None:
    handled: list[str] = []

    def respond(prompt: str, _s):
        import re
        event_id = re.search(r"event_id: (\S+)", prompt).group(1)
        handled.append(event_id)
        publisher.send("builder", "coordinator", "work.error",
                       {"kind": "other", "detail": "done"}, in_reply_to=event_id)
        return "ok"

    builder = ChainWorker(
        "testswarm", "builder", FakeRunner(respond),
        playbook_path=ROLES_DIR / "builder.md",
        workspace=tmp_path, state_dir=tmp_path / "b", client=client,
    )
    builder.start()
    publisher.send("coordinator", "builder", "control.pause",
                   {"role": "builder", "reason": "escalation pending"})
    builder.step(block_ms=1)
    publisher.send("coordinator", "builder", "work.build_requested",
                   {"behaviour_id": "I1.S1.B1", "spec_commit_sha": SHA, "test_paths": ["t.py"]},
                   behaviour_id="I1.S1.B1")
    builder.step(block_ms=1)
    assert handled == []  # parked, not processed

    publisher.send("coordinator", "builder", "control.resume", {"role": "builder"})
    builder.step(block_ms=1)  # consumes the resume
    builder.step(block_ms=1)  # drains the parked work
    assert len(handled) == 1


def test_strikes_escalate_to_interpreter(client, publisher, tmp_path, monkeypatch) -> None:
    sentinel = _sentinel(client, tmp_path)
    sentinel.start()
    for i in range(3):
        publisher.send("specifier", "coordinator", "work.acceptance_verdict",
                       {"behaviour_id": f"I1.S1.B{i + 1}", "verdict": "pass",
                        "run_id": f"run-01J5AB3CDEF4GH5JK6MN7PQ8R{i}"})
        sentinel.step(block_ms=1)
    sentinel.on_tick()
    (escalation,) = _events(client, "sentinel.escalation")
    assert escalation.to_role == "interpreter"
    assert escalation.payload["role"] == "specifier"
    # once escalated, no repeat escalation for the same role
    sentinel.on_tick()
    assert len(_events(client, "sentinel.escalation")) == 1
