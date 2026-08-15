"""A worker is told what it may say and exactly how to say it.

Measured failure (sandtris, 15 Aug): the analyst ran `find /` across the whole
machine — twice — to discover the payload shape of questions.raised and
stories.written, then read the framework's own schema files from inside the
project it was working on. Nothing about that needed a model, a search, or
access to the framework: the worker loads the contract in process.
"""

from pathlib import Path

from relay.contract import load_contract
from relay.contract.cheatsheet import BUDGET, for_role, outgoing, required_fields
from relay.runners.base import TurnResult
from relay.runners.fake import FakeRunner
from relay.workers.chain import ChainWorker

CONTRACT = load_contract()
ROLES = Path(__file__).resolve().parents[2] / "roles"


def test_a_role_is_told_every_type_it_may_publish() -> None:
    text = for_role(CONTRACT, "analyst")
    for type_ in ("questions.raised", "recon.completed", "stories.written"):
        assert type_ in text
    assert "--to interpreter" in text


def test_each_type_comes_with_required_fields_and_a_valid_payload() -> None:
    text = for_role(CONTRACT, "analyst")
    assert "required: question_id, questions" in text
    assert '"question_id": "q-' in text          # a real example, not a description


def test_a_role_is_not_told_what_it_may_not_say() -> None:
    text = for_role(CONTRACT, "builder")
    assert "behaviour.built" in text
    assert "gate.judged" not in text             # not the builder's to send
    assert "worker.started" not in text          # housekeeping the worker does itself


def test_the_vocabulary_stays_bounded() -> None:
    for role in ("interpreter", "analyst", "specifier", "builder", "reviewer", "qa", "security"):
        assert len(for_role(CONTRACT, role)) <= BUDGET + 20, role


def test_infra_roles_have_no_cheatsheet() -> None:
    assert outgoing(CONTRACT, "owner")           # the owner does speak
    assert for_role(CONTRACT, "nobody") == ""


def test_required_fields_are_available_to_the_error_path() -> None:
    assert required_fields(CONTRACT, "stories.written") == ["stories"]
    assert required_fields(CONTRACT, "not.a.type") == []


def test_the_worker_puts_the_vocabulary_in_the_prompt(client, publisher, tmp_path) -> None:
    seen: list[str] = []

    def respond(prompt, session_ref):
        seen.append(prompt)
        return TurnResult(ok=True, session_ref="s1")

    worker = ChainWorker("testswarm", "analyst", FakeRunner(respond),
                         playbook_path=ROLES / "analyst.md",
                         workspace=tmp_path, state_dir=tmp_path / "s", client=client)
    worker.start()
    publisher.send("interpreter", "analyst", "analysis.requested", {"problem": "a sand game"})
    worker.step(block_ms=1)

    assert seen, "the runner was never invoked"
    assert "What you may publish" in seen[0]
    assert "stories.written" in seen[0]
    assert "never read or write outside this project" in seen[0]
