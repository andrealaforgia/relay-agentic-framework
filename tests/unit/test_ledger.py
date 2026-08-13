import json

from relay.bus.keys import ledger_key
from relay.contract.envelope import Envelope
from relay.ledger.audit import audit_ledger
from relay.ledger.export import export_jsonl, import_envelopes, read_jsonl
from relay.ledger.reader import read_all


def _seed(publisher, n=5):
    for i in range(n):
        publisher.send("owner", "interpreter", "problem.stated", {"text": f"p{i}"})


def test_read_all_in_order_across_batches(client, publisher) -> None:
    _seed(publisher, 5)
    envs = [env for _sid, env in read_all(client, "testswarm", batch=2)]
    assert [e.seq for e in envs] == [1, 2, 3, 4, 5]


def test_audit_clean_ledger(client, publisher, validator) -> None:
    _seed(publisher, 3)
    report = audit_ledger(client, validator, "testswarm")
    assert report.ok
    assert report.entries == 3


def test_audit_detects_seq_gap(client, publisher, validator) -> None:
    _seed(publisher, 2)
    # forge a raw XADD around the publisher (what the audit exists to catch)
    env = Envelope.model_validate({
        "swarm": "testswarm", "plane": "chat", "from": "owner", "to": "interpreter",
        "type": "problem.stated", "payload": {"text": "forged"},
        "contract_hash": validator.contract.contract_hash, "seq": 9,
    })
    client.xadd(ledger_key("testswarm"), env.to_fields())
    report = audit_ledger(client, validator, "testswarm")
    assert [f.rule for f in report.findings] == ["seq_gap"]


def test_audit_detects_off_contract_and_drift(client, publisher, validator) -> None:
    _seed(publisher, 1)
    fields = Envelope.model_validate({
        "swarm": "testswarm", "plane": "chat", "from": "builder", "to": "owner",  # forbidden edge
        "type": "update.shared", "payload": {"text": "hi"},
        "contract_hash": "0" * 64, "seq": 2,
    }).to_fields()
    client.xadd(ledger_key("testswarm"), fields)
    report = audit_ledger(client, validator, "testswarm")
    rules = {f.rule for f in report.findings}
    assert rules == {"off_contract", "contract_drift"}


def test_audit_detects_dangling_reply(client, publisher, validator) -> None:
    publisher.send(
        "owner", "interpreter", "problem.stated", {"text": "p"},
        in_reply_to="01J5AB3CDEF4GH5JK6MN7PQ8RS",  # no such earlier event
    )
    report = audit_ledger(client, validator, "testswarm")
    assert [f.rule for f in report.findings] == ["dangling_reply"]


def test_export_import_round_trip(client, publisher, validator, tmp_path) -> None:
    _seed(publisher, 3)
    path = tmp_path / "ledger.jsonl"
    assert export_jsonl(client, "testswarm", path) == 3

    import fakeredis
    fresh = fakeredis.FakeRedis(decode_responses=True)
    assert import_envelopes(fresh, "testswarm", read_jsonl(path)) == 3
    report = audit_ledger(fresh, validator, "testswarm")
    assert report.ok

    # numbering continues after import: next publish gets seq 4
    from relay.bus.publisher import Publisher
    p2 = Publisher(fresh, validator, "testswarm")
    assert p2.send("owner", "interpreter", "problem.stated", {"text": "next"}).seq == 4


def test_import_refuses_non_empty_ledger(client, publisher, tmp_path) -> None:
    _seed(publisher, 1)
    path = tmp_path / "ledger.jsonl"
    export_jsonl(client, "testswarm", path)
    import pytest
    with pytest.raises(ValueError, match="non-empty"):
        import_envelopes(client, "testswarm", read_jsonl(path))


def test_export_is_valid_jsonl(client, publisher, tmp_path) -> None:
    _seed(publisher, 2)
    path = tmp_path / "ledger.jsonl"
    export_jsonl(client, "testswarm", path)
    lines = path.read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["type"] == "problem.stated"
