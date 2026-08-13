class ContractError(Exception):
    """Base class for every contract violation. A raised ContractError means
    the message never reaches the stream (publish side) or goes to the DLQ
    (consume side)."""


class TopologyViolation(ContractError):
    def __init__(self, from_role: str, to_role: str) -> None:
        super().__init__(f"topology violation: {from_role} may not speak to {to_role}")
        self.from_role = from_role
        self.to_role = to_role


class VocabularyViolation(ContractError):
    def __init__(self, from_role: str, to_role: str, type_: str) -> None:
        super().__init__(
            f"vocabulary violation: '{type_}' is not allowed on edge {from_role}>{to_role}"
        )
        self.from_role = from_role
        self.to_role = to_role
        self.type = type_


class PayloadViolation(ContractError):
    def __init__(self, type_: str, detail: str) -> None:
        super().__init__(f"payload violation for '{type_}': {detail}")
        self.type = type_
        self.detail = detail
