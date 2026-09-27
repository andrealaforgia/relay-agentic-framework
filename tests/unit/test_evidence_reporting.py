from test_expectation_workflow import ready, complete_evidence
from test_coordinator import _drive_behaviour_to_done


def test_story_report_exposes_obligations_and_missing_answers(
    client, publisher, monkeypatch
):
    from relay.ledger.evidence import story_report

    swarm = ready(client, publisher)
    _drive_behaviour_to_done(swarm, "I1.S1.B1")
    _drive_behaviour_to_done(swarm, "I1.S1.INT")
    complete_evidence(swarm, monkeypatch)
    report = story_report(swarm.state, "I1.S1")
    assert report["status"] == "awaiting_property_answers"
    assert report["expectations"][0]["source_refs"]
    assert report["properties"][0]["id"] == "I1.S1.P1"
    assert report["receipt"]["checks"]
    assert report["missing_answers"] == ["I1.S1.P1"]


def test_audit_detects_missing_receipt(client, publisher):
    from relay.ledger.audit import audit_ledger
    from relay.contract import ContractValidator, load_contract

    swarm = ready(client, publisher)
    _drive_behaviour_to_done(swarm, "I1.S1.B1")
    _drive_behaviour_to_done(swarm, "I1.S1.INT")
    request = [
        r for r in swarm.sent("run.requested") if r.payload["kind"] == "evidence"
    ][-1]
    publisher.send(
        "toolgate",
        "coordinator",
        "run.completed",
        {
            "run_id": request.payload["run_id"],
            "kind": "evidence",
            "commit_sha": request.payload["commit_sha"],
            "exit_code": 0,
            "duration_s": 1,
            "output_digest": "a" * 64,
            "receipt": {},
            "signature": "forged",
        },
        in_reply_to=request.event_id,
        story_id="I1.S1",
        iteration_id="I1",
    )
    report = audit_ledger(client, ContractValidator(load_contract()), "testswarm")
    assert any(f.rule == "evidence_integrity" for f in report.findings)


def test_evidence_metrics_use_recorded_usage(client, publisher):
    from relay.ledger.evidence import story_metrics
    from relay.ledger.reader import read_all

    publisher.send(
        "analyst",
        "system",
        "usage.reported",
        {
            "role": "analyst",
            "model": "fixture",
            "trigger_type": "story.validation.requested",
            "fresh_session": True,
            "input_tokens": 123,
            "output_tokens": 45,
            "cost_usd": 0.01,
            "duration_s": 2,
        },
        story_id="I1.S1",
    )
    result = story_metrics([env for _, env in read_all(client, "testswarm")], "I1.S1")
    assert result["input_tokens"] == 123
    assert result["output_tokens"] == 45
    assert result["cost_usd"] == 0.01
    assert result["model_turns"] == 1


def test_audit_reports_malformed_roadmap_without_crashing(client, publisher):
    from relay.bus.keys import ledger_key
    from relay.ledger.audit import audit_ledger
    from relay.contract import ContractValidator, load_contract

    swarm = ready(client, publisher)
    original = swarm.sent("story.prepared")[0]
    fields = original.to_fields()
    fields["event_id"] = "01J5AB3CDEF4GH5JK6MN7PQ8RS"
    fields["seq"] = str(client.xlen(ledger_key("testswarm")) + 1)
    fields["type"] = "roadmap.committed"
    fields["from"] = "interpreter"
    fields["plane"] = "plan"
    fields["payload"] = "{}"
    client.xadd(ledger_key("testswarm"), fields)
    report = audit_ledger(client, ContractValidator(load_contract()), "testswarm")
    assert any(f.rule == "off_contract" for f in report.findings)
