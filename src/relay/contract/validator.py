"""Runtime enforcement of the contract: edge, vocabulary, payload.

Called by the publisher before every XADD and by consumers after every read.
A raised ContractError on the publish side means the message never reaches
the stream; on the consume side it means the message goes to the DLQ.
"""

from __future__ import annotations

from typing import Any

from jsonschema import ValidationError

from relay.contract.errors import PayloadViolation, TopologyViolation, VocabularyViolation
from relay.contract.loader import Contract


class ContractValidator:
    def __init__(self, contract: Contract) -> None:
        self._contract = contract

    @property
    def contract(self) -> Contract:
        return self._contract

    def validate_edge(self, from_role: str, to_role: str, type_: str) -> None:
        allowed = self._contract.edges.get((from_role, to_role))
        if allowed is None:
            raise TopologyViolation(from_role, to_role)
        if type_ not in allowed:
            raise VocabularyViolation(from_role, to_role, type_)

    def validate_payload(self, type_: str, payload: dict[str, Any]) -> None:
        validator = self._contract.validators.get(type_)
        if validator is None:
            raise PayloadViolation(type_, "unknown message type")
        try:
            validator.validate(payload)
        except ValidationError as e:
            path = "/".join(str(p) for p in e.absolute_path) or "<root>"
            raise PayloadViolation(type_, f"{e.message} (at {path})") from e

    def validate_message(
        self, from_role: str, to_role: str, type_: str, payload: dict[str, Any]
    ) -> None:
        self.validate_edge(from_role, to_role, type_)
        self.validate_payload(type_, payload)

    def plane_of(self, type_: str) -> str:
        try:
            return self._contract.type_planes[type_]
        except KeyError:
            raise PayloadViolation(type_, "unknown message type") from None
