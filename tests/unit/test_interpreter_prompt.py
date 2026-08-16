"""The Interpreter is told where its tools are and what it may publish.

From a fresh scopa session: 53 API calls, of which roughly half the tool
calls were the interpreter looking for its own commands (`which relay-send`,
`find / -maxdepth 4 -iname "relay*"`) and its own payload schemas
(contract/schema/update.shared.json), then running everything as
`cd .../relay-agentic-framework && ./.venv/bin/relay-send …`. Every one of
those searches re-sends the whole conversation, so hunting costs the same per
call as thinking.
"""

from relay.cli.main import NATIVE_SESSION_SUFFIX
from relay.contract import load_contract
from relay.contract.cheatsheet import for_role


def _prompt(send="/opt/bin/relay-send", ident="/opt/bin/relay-id",
            inbox="/opt/bin/relay-inbox") -> str:
    return NATIVE_SESSION_SUFFIX.format(swarm="scopa", send=send, id=ident, inbox=inbox) \
        + for_role(load_contract(), "interpreter")


def test_the_commands_are_absolute_so_nothing_is_hunted() -> None:
    text = _prompt()
    assert "/opt/bin/relay-send" in text
    assert "/opt/bin/relay-id q" in text
    assert "/opt/bin/relay-inbox --swarm scopa" in text
    assert "do not go looking for them" in text


def test_it_knows_every_type_it_may_publish() -> None:
    text = _prompt()
    for type_ in ("questions.asked", "roadmap.proposed", "checkpoint.reached",
                  "analysis.requested", "roadmap.committed"):
        assert type_ in text, type_
    assert "required: question_id, questions" in text


def test_it_is_told_the_boundary() -> None:
    assert "framework's own source is not yours" in _prompt()


def test_the_owner_facing_rules_survive() -> None:
    text = _prompt()
    assert "--wait 240" in text and "Never leave the Owner" in text
