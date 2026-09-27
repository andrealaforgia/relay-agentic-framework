"""Real code, real receipts: an example passes while a story property fails."""

import sys

from test_toolgate_and_coordinator import project as project
from test_toolgate_and_coordinator import _git
from relay.bus.keys import ledger_key
from relay.contract.envelope import Envelope
from relay.coordinator.main import Coordinator
from relay.workers.toolgate import Toolgate


def test_faulty_story_is_refused_then_verified_after_repair(
    client, publisher, project, monkeypatch
):
    monkeypatch.setenv("RELAY_EVIDENCE_ALLOW_NATIVE", "1")
    policy = project / "policy.yaml"
    policy.write_text("plan_required: false\n")
    coordinator = Coordinator("testswarm", project, policy_path=policy, client=client)
    gate = Toolgate(
        "testswarm",
        project,
        commands={
            "acceptance_test": f"{sys.executable} -m pytest -q {{test_paths}}",
            "evidence": f"{sys.executable} -m pytest -q",
        },
        client=client,
    )

    def events(kind):
        return [
            Envelope.from_fields(f)
            for _, f in client.xrange(ledger_key("testswarm"))
            if f["type"] == kind
        ]

    def pump():
        for _ in range(50):
            if not coordinator.step(block_ms=1):
                return
        raise AssertionError("coordinator did not reach a fixed point")

    def commit(message):
        _git(project, "add", "-A")
        _git(project, "commit", "-qm", message)
        return _git(project, "rev-parse", "HEAD")

    def run():
        request = events("run.requested")[-1]
        gate.handle(request)
        pump()
        return events("run.completed")[-1]

    def answer():
        request = events("story.validation.requested")[-1]
        publisher.send(
            "analyst",
            "coordinator",
            "story.validation.judged",
            {
                "story_id": "I1.S1",
                "version": 1,
                "commit_sha": request.payload["commit_sha"],
                "run_id": request.payload["run_id"],
                "answers": [
                    {
                        "property_id": "I1.S1.P1",
                        "verdict": "supported",
                        "run_id": request.payload["run_id"],
                        "explanation": "All seven booking-state combinations passed through the CLI",
                    }
                ],
            },
            in_reply_to=request.event_id,
        )
        pump()

    source = publisher.send(
        "owner",
        "interpreter",
        "problem.stated",
        {"text": "List only free rooms, including an empty building."},
    )
    criterion = "Given booked and free rooms, list only the free rooms."
    publisher.send(
        "interpreter",
        "coordinator",
        "roadmap.committed",
        {
            "intake": {"mode": "greenfield"},
            "roadmap": {
                "protocol_version": 2,
                "iterations": [
                    {
                        "id": "I1",
                        "goal": "Find rooms",
                        "increment": "A rooms CLI",
                        "stories": [
                            {
                                "id": "I1.S1",
                                "title": "Free rooms",
                                "narrative": "See which rooms are free",
                                "acceptance_criteria": [
                                    {"id": "I1.S1.B1", "text": criterion}
                                ],
                            }
                        ],
                    }
                ],
            },
        },
    )
    publisher.send(
        "interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"}
    )
    coordinator.bootstrap()
    pump()
    preparation = events("story.preparation.requested")[-1]
    publisher.send(
        "analyst",
        "coordinator",
        "story.prepared",
        {
            "story_id": "I1.S1",
            "version": 1,
            "expectations": [
                {
                    "id": "I1.S1.B1",
                    "text": criterion,
                    "source_refs": [
                        {"event_id": source.event_id, "quote": "only free rooms"}
                    ],
                }
            ],
            "properties": [
                {
                    "id": "I1.S1.P1",
                    "question": "For every building of up to two rooms, are exactly its free rooms listed?",
                    "scope": "Exhaustive boolean booking states for zero, one and two rooms",
                    "expectation_ids": ["I1.S1.B1"],
                    "verification": "Execute the CLI on all seven states",
                }
            ],
        },
        in_reply_to=preparation.event_id,
    )
    pump()
    tests = project / "tests"
    tests.mkdir()
    property_test = tests / "test_properties.py"
    property_test.write_text("""import itertools, json, subprocess, sys
import pytest
STATES = [list(states) for n in range(3) for states in itertools.product([False, True], repeat=n)]
@pytest.mark.parametrize("rooms", STATES)
def test_free_rooms(rooms):
    result = subprocess.run([sys.executable, "rooms.py", json.dumps(rooms)], capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout) == [i for i, free in enumerate(rooms) if free]
""")
    property_sha = commit("independent property checks")
    publisher.send(
        "specifier",
        "coordinator",
        "story.verification.written",
        {
            "story_id": "I1.S1",
            "version": 1,
            "commit_sha": property_sha,
            "checks": {"I1.S1.P1": ["tests/test_properties.py::test_free_rooms"]},
        },
        in_reply_to=events("story.verification.requested")[-1].event_id,
    )
    pump()
    publisher.send(
        "qa",
        "coordinator",
        "story.verification.reviewed",
        {
            "story_id": "I1.S1",
            "version": 1,
            "verdict": "pass",
            "reason": "Exhausts the stated domain through the public CLI",
        },
        in_reply_to=events("story.verification.review.requested")[-1].event_id,
    )
    pump()
    acceptance = """import json, subprocess, sys
def test_free_rooms():
    result = subprocess.run([sys.executable, "rooms.py", "[false, true]"], capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout) == [1]
"""
    (tests / "test_acceptance.py").write_text(acceptance)
    spec_sha = commit("acceptance example")
    publisher.send(
        "specifier",
        "coordinator",
        "spec.written",
        {
            "behaviour_id": "I1.S1.B1",
            "test_paths": ["tests/test_acceptance.py"],
            "commit_sha": spec_sha,
            "touches": ["rooms.py"],
        },
    )
    pump()
    assert run().payload["exit_code"] != 0
    (project / "rooms.py").write_text(
        "import json,sys\nrooms=json.loads(sys.argv[1])\nprint(json.dumps([i for i,free in enumerate(rooms) if free] if rooms else [99]))\n"
    )
    faulty = commit("example passes but empty building is wrong")
    publisher.send(
        "builder",
        "coordinator",
        "behaviour.built",
        {
            "behaviour_id": "I1.S1.B1",
            "story_id": "I1.S1",
            "iteration_id": "I1",
            "commit_sha": faulty,
            "attempt": 1,
        },
    )
    pump()
    assert run().payload["exit_code"] == 0
    publisher.send(
        "specifier",
        "coordinator",
        "acceptance.judged",
        {
            "behaviour_id": "I1.S1.B1",
            "verdict": "pass",
            "run_id": events("judgement.requested")[-1].payload["run_id"],
        },
    )
    pump()
    publisher.send(
        "specifier",
        "coordinator",
        "spec.written",
        {
            "behaviour_id": "I1.S1.INT",
            "test_paths": ["tests/test_acceptance.py"],
            "commit_sha": faulty,
            "touches": [],
        },
    )
    pump()
    run()
    failed_receipt = run()
    assert (
        failed_receipt.payload["kind"] == "evidence"
        and failed_receipt.payload["exit_code"] != 0
    )
    assert not events("story.completed")
    assert "rooms0" in failed_receipt.payload["receipt"]["stdout"]
    rework = events("rework.requested")[-1]
    assert rework.to_role == "builder"
    (project / "rooms.py").write_text(
        "import json,sys\nrooms=json.loads(sys.argv[1])\nprint(json.dumps([i for i,free in enumerate(rooms) if free]))\n"
    )
    fixed = commit("correct the empty building")
    publisher.send(
        "builder",
        "coordinator",
        "behaviour.built",
        {
            "behaviour_id": "I1.S1.INT",
            "story_id": "I1.S1",
            "iteration_id": "I1",
            "commit_sha": fixed,
            "attempt": rework.payload["attempt"],
        },
        in_reply_to=rework.event_id,
    )
    pump()
    run()
    publisher.send(
        "specifier",
        "coordinator",
        "acceptance.judged",
        {
            "behaviour_id": "I1.S1.INT",
            "verdict": "pass",
            "run_id": events("judgement.requested")[-1].payload["run_id"],
        },
    )
    pump()
    successful = run()
    assert successful.payload["exit_code"] == 0
    assert len(successful.payload["receipt"]["coverage"]["I1.S1.P1"]) == 7
    answer()
    assert events("story.completed")
    publisher.send(
        "specifier",
        "coordinator",
        "spec.written",
        {
            "behaviour_id": "I1.INT",
            "test_paths": ["tests/test_acceptance.py"],
            "commit_sha": fixed,
            "touches": [],
        },
    )
    pump()
    run()
    # The integration check uses the same candidate; existing story evidence
    # still needs to cover the newly introduced integration obligation.
    assert not events("iteration.finished")
    assert events("run.requested")[-1].payload["kind"] == "evidence"
    assert "I1.INT" in events("run.requested")[-1].payload["evidence"]["bindings"]
    run()
    answer()
    assert events("iteration.finished")
