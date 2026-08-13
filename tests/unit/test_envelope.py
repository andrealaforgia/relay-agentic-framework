import pytest
from pydantic import ValidationError

from relay.contract.envelope import Envelope, new_event_id

HASH = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


def make_envelope(**overrides: object) -> Envelope:
    data: dict = {
        "swarm": "acme",
        "plane": "work",
        "from": "builder",
        "to": "coordinator",
        "type": "behaviour.built",
        "contract_hash": HASH,
        "payload": {"behaviour_id": "I1.S1.B1"},
    }
    data.update(overrides)
    return Envelope.model_validate(data)


def test_round_trip_preserves_everything() -> None:
    env = make_envelope(
        seq=7,
        in_reply_to=new_event_id(),
        behaviour_id="I1.S1.B1",
        commit_sha="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
    )
    restored = Envelope.from_fields(env.to_fields())
    assert restored == env


def test_optional_fields_are_empty_strings_on_wire() -> None:
    fields = make_envelope().to_fields()
    assert fields["in_reply_to"] == ""
    assert fields["gate_id"] == ""
    assert Envelope.from_fields(fields).in_reply_to is None


def test_event_id_is_ulid_shaped_and_unique() -> None:
    a, b = make_envelope(), make_envelope()
    assert a.event_id != b.event_id
    assert len(a.event_id) == 26


def test_bad_in_reply_to_rejected() -> None:
    with pytest.raises(ValidationError):
        make_envelope(in_reply_to="not-a-ulid")


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        make_envelope(refs="I1,B1")  # v1's stringly refs are unrepresentable
