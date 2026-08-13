import pytest

from relay.bus.keys import ledger_key
from relay.bus.publisher import Publisher
from relay.contract import PayloadViolation, TopologyViolation
from relay.contract.envelope import Envelope


def test_publish_assigns_gap_free_seq(publisher, client) -> None:
    results = [
        publisher.send("owner", "interpreter", "chat.problem", {"text": f"p{i}"})
        for i in range(3)
    ]
    assert [r.seq for r in results] == [1, 2, 3]
    entries = client.xrange(ledger_key("testswarm"))
    assert [int(fields["seq"]) for _id, fields in entries] == [1, 2, 3]


def test_off_contract_edge_never_reaches_stream(publisher, client) -> None:
    with pytest.raises(TopologyViolation):
        publisher.send("builder", "owner", "chat.result", {"text": "hi"})
    assert client.xlen(ledger_key("testswarm")) == 0
    # and the seq counter was never consumed — no gap manufactured by a failed publish
    ok = publisher.send("owner", "interpreter", "chat.problem", {"text": "p"})
    assert ok.seq == 1


def test_bad_payload_never_reaches_stream(publisher, client) -> None:
    with pytest.raises(PayloadViolation):
        publisher.send("owner", "interpreter", "chat.problem", {})
    assert client.xlen(ledger_key("testswarm")) == 0


def test_wrong_plane_rejected(publisher, validator) -> None:
    env = Envelope.model_validate({
        "swarm": "testswarm",
        "plane": "control",  # chat.problem lives on the chat plane
        "from": "owner",
        "to": "interpreter",
        "type": "chat.problem",
        "payload": {"text": "hello"},
        "contract_hash": validator.contract.contract_hash,
    })
    with pytest.raises(PayloadViolation, match="plane"):
        publisher.publish(env)


def test_contract_drift_rejected_at_publish(publisher) -> None:
    env = Envelope.model_validate({
        "swarm": "testswarm",
        "plane": "chat",
        "from": "owner",
        "to": "interpreter",
        "type": "chat.problem",
        "payload": {"text": "hello"},
        "contract_hash": "0" * 64,  # a different contract
    })
    with pytest.raises(PayloadViolation, match="contract drift"):
        publisher.publish(env)


def test_wrong_swarm_rejected(publisher, validator) -> None:
    env = Envelope.model_validate({
        "swarm": "otherswarm",
        "plane": "chat",
        "from": "owner",
        "to": "interpreter",
        "type": "chat.problem",
        "payload": {"text": "hello"},
        "contract_hash": validator.contract.contract_hash,
    })
    with pytest.raises(PayloadViolation, match="swarm"):
        publisher.publish(env)


def test_round_trip_through_stream(publisher, client) -> None:
    publisher.send(
        "builder", "coordinator", "work.built",
        {
            "behaviour_id": "I1.S1.B1", "story_id": "I1.S1", "iteration_id": "I1",
            "commit_sha": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0", "attempt": 1,
        },
        behaviour_id="I1.S1.B1",
        commit_sha="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
    )
    ((_id, fields),) = client.xrange(ledger_key("testswarm"))
    env = Envelope.from_fields(fields)
    assert env.type == "work.built"
    assert env.seq == 1
    assert env.payload["attempt"] == 1
    assert env.behaviour_id == "I1.S1.B1"
