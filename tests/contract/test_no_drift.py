"""The generated artifacts must byte-match a fresh generation.

A hand edit to contract/schema/*.json or docs/PROTOCOL.md, or a contract
change committed without regenerating (`relay contract gen`), fails here.
"""

from relay.contract import load_contract
from relay.contract.codegen import generate_protocol_doc, generate_schemas
from relay.contract.loader import REPO_ROOT

contract = load_contract()


def test_schema_files_match_generation() -> None:
    schema_dir = REPO_ROOT / "contract" / "schema"
    generated = generate_schemas(contract)
    on_disk = {p.stem: p.read_text() for p in schema_dir.glob("*.json")}
    assert on_disk == generated, (
        "contract/schema/ is out of date — run `relay contract gen` and commit the result"
    )


def test_protocol_doc_matches_generation() -> None:
    on_disk = (REPO_ROOT / "docs" / "PROTOCOL.md").read_text()
    assert on_disk == generate_protocol_doc(contract), (
        "docs/PROTOCOL.md is out of date — run `relay contract gen` and commit the result"
    )
