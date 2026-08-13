"""Generate derived artifacts from the contract.

Two outputs, both committed and both byte-diffed in CI (tests/contract/test_no_drift.py):
  - contract/schema/<type>.json  — one resolved JSON Schema per message type
  - docs/PROTOCOL.md             — the human-readable edge/type matrix

Nothing hand-written duplicates the contract; these files exist for IDEs,
external validators, and readers — the runtime always uses the YAML directly.
"""

from __future__ import annotations

import json
from pathlib import Path

from relay.contract.loader import Contract, REPO_ROOT


def generate_schemas(contract: Contract) -> dict[str, str]:
    """type -> pretty JSON schema text."""
    return {
        type_: json.dumps(schema, indent=2, sort_keys=True) + "\n"
        for type_, schema in contract.payload_schemas.items()
    }


def generate_protocol_doc(contract: Contract) -> str:
    lines: list[str] = [
        "# Relay Protocol",
        "",
        "**GENERATED from `contract/relay-contract.yaml` — do not edit.**",
        f"Contract version {contract.version}, hash `{contract.contract_hash}`.",
        "",
        "## Roles",
        "",
        f"- Assistants: {', '.join(contract.assistants)}",
        f"- Humans: {', '.join(contract.humans)}",
        f"- Infra: {', '.join(contract.infra)}",
        "",
        "## Edges",
        "",
        "| Plane | Edge | Message types |",
        "|---|---|---|",
    ]
    def sort_key(item: tuple[tuple[str, str], frozenset[str]]) -> tuple[int, str, str]:
        edge, _ = item
        plane = contract.edge_planes[edge]
        return (contract.planes.index(plane), edge[0], edge[1])

    for edge, types in sorted(contract.edges.items(), key=sort_key):
        plane = contract.edge_planes[edge]
        lines.append(f"| {plane} | `{edge[0]}>{edge[1]}` | {', '.join(f'`{t}`' for t in sorted(types))} |")

    lines += [
        "",
        "## Message types",
        "",
        "One JSON Schema per type lives in `contract/schema/`.",
        "",
        "| Type | Plane | Required payload fields |",
        "|---|---|---|",
    ]
    for type_ in sorted(contract.payload_schemas):
        schema = contract.payload_schemas[type_]
        required = ", ".join(f"`{f}`" for f in schema.get("required", []))
        lines.append(f"| `{type_}` | {contract.type_planes[type_]} | {required} |")
    lines.append("")
    return "\n".join(lines)


def write_artifacts(contract: Contract, repo_root: Path | None = None) -> list[Path]:
    root = repo_root or REPO_ROOT
    written: list[Path] = []

    schema_dir = root / "contract" / "schema"
    schema_dir.mkdir(parents=True, exist_ok=True)
    schemas = generate_schemas(contract)
    for stale in schema_dir.glob("*.json"):
        if stale.stem not in schemas:
            stale.unlink()
    for type_, text in schemas.items():
        path = schema_dir / f"{type_}.json"
        path.write_text(text)
        written.append(path)

    protocol = root / "docs" / "PROTOCOL.md"
    protocol.parent.mkdir(parents=True, exist_ok=True)
    protocol.write_text(generate_protocol_doc(contract))
    written.append(protocol)
    return written
