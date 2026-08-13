"""Every message type must have an example, and every example must validate.

This is the test that makes "I added a type but forgot the example" (or the
schema and example drifting apart) loud.
"""

from pathlib import Path

import pytest
import yaml

from relay.contract import ContractValidator, load_contract
from relay.contract.loader import REPO_ROOT

EXAMPLES_PATH = REPO_ROOT / "contract" / "examples.yaml"

contract = load_contract()
validator = ContractValidator(contract)
examples: dict[str, dict] = yaml.safe_load(EXAMPLES_PATH.read_text())


def test_every_type_has_an_example() -> None:
    missing = sorted(contract.message_types - set(examples))
    assert not missing, f"message types without an example payload: {missing}"


def test_no_example_for_unknown_type() -> None:
    unknown = sorted(set(examples) - contract.message_types)
    assert not unknown, f"examples for types not in the contract: {unknown}"


@pytest.mark.parametrize("type_", sorted(examples))
def test_example_validates(type_: str) -> None:
    validator.validate_payload(type_, examples[type_])
