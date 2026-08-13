import pytest

from relay.contract import (
    ContractValidator,
    PayloadViolation,
    TopologyViolation,
    VocabularyViolation,
    load_contract,
)

contract = load_contract()
validator = ContractValidator(contract)


def test_allowed_edge_and_type_passes() -> None:
    validator.validate_edge("owner", "interpreter", "problem.stated")
    validator.validate_edge("builder", "coordinator", "behaviour.built")


def test_unlisted_edge_is_topology_violation() -> None:
    # The builder must never speak to the owner — the core realm rule.
    with pytest.raises(TopologyViolation):
        validator.validate_edge("builder", "owner", "update.shared")
    with pytest.raises(TopologyViolation):
        validator.validate_edge("specifier", "builder", "spec.written")  # goes via coordinator


def test_wrong_type_on_edge_is_vocabulary_violation() -> None:
    with pytest.raises(VocabularyViolation):
        validator.validate_edge("owner", "interpreter", "behaviour.built")


def test_wildcard_edges_expanded() -> None:
    # sentinel may correct every assistant, and every assistant may ack.
    for assistant in contract.assistants:
        if assistant == "sentinel":
            continue
        validator.validate_edge("sentinel", assistant, "correction.issued")
        validator.validate_edge(assistant, "sentinel", "correction.acknowledged")
    # every role may report to system
    validator.validate_edge("toolgate", "system", "worker.started")
    validator.validate_edge("owner", "system", "worker.stopped")


def test_self_edges_never_exist() -> None:
    for from_role, to_role in contract.edges:
        assert from_role != to_role


def test_bad_payload_is_payload_violation() -> None:
    with pytest.raises(PayloadViolation):
        validator.validate_payload("problem.stated", {})  # 'text' required
    with pytest.raises(PayloadViolation):
        validator.validate_payload("behaviour.built", {
            "behaviour_id": "I1.S1.B1",
            "story_id": "I1.S1",
            "iteration_id": "I1",
            "commit_sha": "tooshort",  # not a 40-char sha
            "attempt": 1,
        })
    with pytest.raises(PayloadViolation):
        validator.validate_payload("problem.stated", {"text": "hi", "extra": True})  # additionalProperties


def test_unknown_type_is_payload_violation() -> None:
    with pytest.raises(PayloadViolation):
        validator.validate_payload("work.done", {})
    with pytest.raises(PayloadViolation):
        validator.plane_of("work.done")


def test_correction_note_cannot_carry_work_content() -> None:
    # The control plane is structurally incapable of smuggling work: >500 chars rejected.
    payload = {
        "finding_id": "find-01J5AB3CDEF4GH5JK6MN7PQ8RS",
        "subject_event_id": "01J5AB3CDEF4GH5JK6MN7PQ8RS",
        "rule_id": "realm.builder",
        "required_remedy": "resend_on_contract",
        "note": "x" * 501,
    }
    with pytest.raises(PayloadViolation):
        validator.validate_payload("correction.issued", payload)


def test_contract_hash_shape_and_stability() -> None:
    a = load_contract().contract_hash
    b = load_contract().contract_hash
    assert a == b
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)
