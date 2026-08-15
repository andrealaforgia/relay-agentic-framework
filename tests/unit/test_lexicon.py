"""The roadmap vocabulary, checked in code rather than remembered (D1).

The detector is narrow on purpose: it fires on words used as COUNTABLE UNITS
OF WORK, not on the same words doing honest domain duty. A tic-tac-toe swarm
must be free to say "best of three rounds" about the game while never
promising the Owner "a first round of work".
"""

from pathlib import Path

import pytest

from relay.lexicon import NON_CONTRACT_UNITS, correction_note, counted_units, scan_payload

ROLES = Path(__file__).resolve().parents[2] / "roles"


@pytest.mark.parametrize("text", [
    "In the first round we deliver the basics",
    "Phase 2 adds the scoreboard",
    "We planned three sprints",
    "the next milestone is a working game",
    "each step delivers something usable",
    "this batch covers the rules",
])
def test_invented_units_of_work_are_caught(text: str) -> None:
    assert counted_units(text), text


@pytest.mark.parametrize("text", [
    "Iteration I1 delivers a playable game",
    "Story I1.S1 lets you place a mark",
    "Behaviour I1.S1.B1 rejects an occupied square",
    "The winner is whoever takes three squares in a row",
    "A roundtrip through the API stays under a second",   # substring, not the word
    "Players alternate until the board is full",
])
def test_contract_vocabulary_and_ordinary_prose_pass(text: str) -> None:
    assert counted_units(text) == [], text


def test_the_owners_domain_may_still_have_rounds() -> None:
    """A false positive we accept and hand to the model to judge: the note
    tells it to acknowledge when the phrase is genuinely the domain's."""
    text = "Best of three rounds wins the match"
    assert counted_units(text)                       # detected...
    assert "domain vocabulary" in correction_note(counted_units(text))   # ...but not accused


def test_nested_payloads_are_scanned_whole() -> None:
    payload = {
        "narrative": "A playable game first",
        "roadmap": {"iterations": [
            {"id": "I1", "goal": "Play a game",
             "stories": [{"title": "Second phase of scoring", "narrative": "As a player…"}]},
        ]},
    }
    assert scan_payload(payload) == ["second phase"]


def test_the_same_phrase_is_reported_once() -> None:
    assert scan_payload({"a": "the first round", "b": "The First Round again"}) == ["first round"]


def test_the_playbooks_ban_exactly_what_the_code_catches() -> None:
    """The prose the models read and the check that corrects them must not
    drift apart — that is how a rule becomes folklore."""
    for playbook in ("analyst.md", "interpreter.md"):
        text = (ROLES / playbook).read_text().lower()
        missing = [word for word in NON_CONTRACT_UNITS if f'"{word}"' not in text]
        assert not missing, f"{playbook} does not ban: {missing}"
