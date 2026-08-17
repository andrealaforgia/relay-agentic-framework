"""Load and index contract/relay-contract.yaml — the single source of truth.

The loaded `Contract` is immutable for the life of the process. Wildcard edges
("coordinator>*assistant", "*>system") are expanded exactly once, here, so every
other module works with concrete edges only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONTRACT_PATH = REPO_ROOT / "contract" / "relay-contract.yaml"

_ASSISTANT_WILDCARD = "*assistant"
_ANY_WILDCARD = "*"


@dataclass(frozen=True, eq=False)
class Contract:
    version: int
    contract_hash: str
    assistants: tuple[str, ...]
    humans: tuple[str, ...]
    infra: tuple[str, ...]
    planes: tuple[str, ...]
    # (from_role, to_role) -> allowed types on that edge
    edges: dict[tuple[str, str], frozenset[str]]
    # (from_role, to_role) -> plane the edge belongs to
    edge_planes: dict[tuple[str, str], str]
    # message type -> plane
    type_planes: dict[str, str]
    # message type -> full JSON schema (with $defs injected)
    payload_schemas: dict[str, dict[str, Any]]
    # message type -> compiled validator
    validators: dict[str, Draft202012Validator]

    @property
    def all_roles(self) -> tuple[str, ...]:
        return self.assistants + self.humans + self.infra

    @property
    def message_types(self) -> frozenset[str]:
        return frozenset(self.payload_schemas)


def compute_contract_hash(raw: dict[str, Any]) -> str:
    """sha256 over the canonical JSON form of the parsed YAML.

    Canonical JSON (sorted keys, no whitespace variance) rather than file
    bytes, so a comment or reformat does not count as a different contract
    while any semantic change does.
    """
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _expand_endpoint(token: str, assistants: tuple[str, ...], all_roles: tuple[str, ...]) -> list[str]:
    if token == _ASSISTANT_WILDCARD:
        return list(assistants)
    if token == _ANY_WILDCARD:
        return [r for r in all_roles if r != "system"]
    return [token]


def load_contract(path: Path | None = None) -> Contract:
    contract_path = path or DEFAULT_CONTRACT_PATH
    raw = yaml.safe_load(contract_path.read_text())

    assistants = tuple(raw["roles"]["assistants"])
    humans = tuple(raw["roles"]["humans"])
    infra = tuple(raw["roles"]["infra"])
    all_roles = assistants + humans + infra
    planes = tuple(raw["planes"])
    defs: dict[str, Any] = raw["defs"]

    edges: dict[tuple[str, str], set[str]] = {}
    edge_planes: dict[tuple[str, str], str] = {}
    type_planes: dict[str, str] = {}

    for plane, plane_edges in raw["edges"].items():
        for edge_key, types in plane_edges.items():
            from_token, to_token = edge_key.split(">", 1)
            for from_role in _expand_endpoint(from_token, assistants, all_roles):
                for to_role in _expand_endpoint(to_token, assistants, all_roles):
                    if from_role == to_role:
                        continue
                    edge = (from_role, to_role)
                    edges.setdefault(edge, set()).update(types)
                    edge_planes.setdefault(edge, plane)
            for type_ in types:
                type_planes.setdefault(type_, plane)

    payload_schemas: dict[str, dict[str, Any]] = {}
    validators: dict[str, Draft202012Validator] = {}
    for type_, spec in raw["message_types"].items():
        schema = dict(spec["payload_schema"])
        schema["$defs"] = defs
        Draft202012Validator.check_schema(schema)
        payload_schemas[type_] = schema
        validators[type_] = Draft202012Validator(schema)

    # Every type referenced on an edge must have a schema, and vice versa.
    edge_types = set(type_planes)
    schema_types = set(payload_schemas)
    if edge_types != schema_types:
        missing_schema = sorted(edge_types - schema_types)
        missing_edge = sorted(schema_types - edge_types)
        raise ValueError(
            f"contract is self-inconsistent: types on edges without schema {missing_schema}; "
            f"types with schema on no edge {missing_edge}"
        )

    return Contract(
        version=int(raw["contract_version"]),
        contract_hash=compute_contract_hash(raw),
        assistants=assistants,
        humans=humans,
        infra=infra,
        planes=planes,
        edges={edge: frozenset(types) for edge, types in edges.items()},
        edge_planes=edge_planes,
        type_planes=type_planes,
        payload_schemas=payload_schemas,
        validators=validators,
    )
