import json
from pathlib import Path

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


def _forge(client, validator, seq: int, contract_hash: str, type_: str,
           payload: dict, frm: str = "owner", to: str = "interpreter") -> None:
    """An entry the publisher did not write — the only way to control both the
    seq and the contract hash, which is what these two tests are about."""
    client.xadd(ledger_key("testswarm"), Envelope.model_validate({
        "swarm": "testswarm", "plane": validator.plane_of(type_), "from": frm,
        "to": to, "type": type_, "payload": payload,
        "contract_hash": contract_hash, "seq": seq,
    }).to_fields())


def test_a_declared_contract_upgrade_clears_the_history_before_it(
    client, validator
) -> None:
    """A contract bump must not turn a clean ledger into one finding per event.

    friend-positions bumped its contract and `relay audit` flagged all 2248
    preceding entries: the old hash was only learned when the scan reached the
    contract.upgraded event, by which point the whole past had already been
    judged against a set that could not contain it. The declaration was inert
    — the pass that reads it has to come first.
    """
    old, new = "b" * 64, validator.contract.contract_hash
    _forge(client, validator, 1, old, "problem.stated", {"text": "under the old hash"})
    assert {f.rule for f in audit_ledger(client, validator, "testswarm").findings} \
        == {"contract_drift"}, "undeclared, it is a real finding"

    _forge(client, validator, 2, new, "contract.upgraded",
           {"old_hash": old, "new_hash": new}, frm="coordinator", to="system")
    assert audit_ledger(client, validator, "testswarm").ok, \
        "the declaration arrives last and must still cover what came first"


def test_an_undeclared_hash_is_still_reported(client, validator) -> None:
    """The pre-pass widens what counts as known; it must not wave drift through.
    friend-positions really did run two contracts side by side for one minute
    on its first day, and the audit must keep saying so."""
    old, new = "b" * 64, validator.contract.contract_hash
    _forge(client, validator, 1, new, "contract.upgraded",
           {"old_hash": old, "new_hash": new}, frm="coordinator", to="system")
    _forge(client, validator, 2, "c" * 64, "problem.stated", {"text": "a third hash"})
    findings = audit_ledger(client, validator, "testswarm").findings
    assert [f.rule for f in findings] == ["contract_drift"]
    assert "cccccccccccc" in findings[0].detail


def test_a_declaration_does_not_whitelist_the_old_hash_forever(
    client, validator
) -> None:
    """A contract.upgraded says "what came before me was written under the old
    contract" — not "this hash is fine from now on".

    friend-positions really did run two contracts side by side on its first
    day, one process a version behind. Accepting the old hash globally makes
    exactly that straggler invisible, which is the drift the audit exists for.
    """
    old, new = "b" * 64, validator.contract.contract_hash
    _forge(client, validator, 1, old, "problem.stated", {"text": "before"})
    _forge(client, validator, 2, new, "contract.upgraded",
           {"old_hash": old, "new_hash": new}, frm="coordinator", to="system")
    assert audit_ledger(client, validator, "testswarm").ok

    _forge(client, validator, 3, old, "problem.stated", {"text": "a straggler, after"})
    findings = audit_ledger(client, validator, "testswarm").findings
    assert [(f.seq, f.rule) for f in findings] == [(3, "contract_drift")]


def test_stream_ids_are_ordered_as_numbers_not_strings() -> None:
    """`…-10` sorts before `…-2` as text, which would put a tenth entry in the
    same millisecond on the wrong side of the declaration."""
    from relay.ledger.audit import _at

    assert _at("1787249757332-2") < _at("1787249757332-10")
    assert _at("999-0") < _at("1000-0")
    assert _at("nonsense") == (0, 0)


def test_the_coordinator_declares_its_own_contract_transition(client, validator) -> None:
    """`relay up` starts every process at once, so by the time the coordinator
    has folded the ledger the workers have already announced themselves on the
    new contract. Reading "the last hash" therefore sees its own version, calls
    the transition already declared, and publishes nothing — measured against a
    real swarm, where it silently skipped a genuine bump.

    It also must not speak for hashes that were never its own: a rogue worker's
    contract is drift for the audit to report, not something to whitelist.
    """
    from relay.coordinator.main import Coordinator

    old, mine = "b" * 64, validator.contract.contract_hash
    _forge(client, validator, 1, old, "progress.reported",
           {"iteration_id": "I1", "behaviours_done": 0, "behaviours_total": 1},
           frm="coordinator", to="owner")
    _forge(client, validator, 2, "c" * 64, "problem.stated", {"text": "a rogue worker"})
    _forge(client, validator, 3, mine, "problem.stated", {"text": "a worker, already upgraded"})

    coordinator = Coordinator("testswarm", Path("/tmp"), client=client)
    coordinator.bootstrap()

    upgrades = [json.loads(f["payload"]) for _s, f in client.xrange(ledger_key("testswarm"))
                if f["type"] == "contract.upgraded"]
    assert [(u["old_hash"], u["new_hash"]) for u in upgrades] == [(old, mine)], \
        "its own predecessor, and only that"
