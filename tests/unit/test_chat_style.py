"""The Owner gets a few sentences, and never a blocker they cannot answer.

Both failures are silent: a wall of text is not an error, and an option-less
question validates against the contract perfectly well. The Owner just stops
answering — which is exactly what happened when three escalations sat
unanswered on a live run.
"""

from pathlib import Path

from relay.chat_style import MAX_OWNER_TEXT, overlong, unanswerable_questions
from relay.runners.fake import FakeRunner
from relay.workers.sentinel import SentinelWorker

ROLES = Path(__file__).resolve().parents[2] / "roles"


def _sentinel(client, tmp_path):
    return SentinelWorker("testswarm", runner=FakeRunner(lambda _p, _s: "clean"),
                          playbook_path=ROLES / "sentinel.md", workspace=tmp_path,
                          state_dir=tmp_path / "s", client=client)


def _corrections(client):
    from relay.bus.keys import ledger_key
    from relay.contract.envelope import Envelope
    return [Envelope.from_fields(f) for _s, f in client.xrange(ledger_key("testswarm"))
            if f["type"] == "correction.issued"]


def test_a_few_sentences_passes_a_wall_of_text_does_not() -> None:
    assert overlong("update.shared", {"text": "Story I1.S1 is done. Try it: open index.html."}) == 0
    assert overlong("checkpoint.reached", {"summary": "x" * (MAX_OWNER_TEXT + 50)}) == 50
    # nothing else is length-checked: a roadmap narrative is allowed to breathe
    assert overlong("roadmap.proposed", {"narrative": "y" * 5000}) == 0


def test_a_question_without_options_is_unanswerable() -> None:
    assert unanswerable_questions({"questions": [{"text": "What should we do?"}]})
    assert unanswerable_questions(
        {"questions": [{"text": "Which?", "options": ["a", "b"]}]}      # no recommendation
    )
    assert unanswerable_questions(
        {"questions": [{"text": "Which?", "options": ["a", "b"], "recommended": "a"}]}
    ) == []


def test_the_sentinel_corrects_a_wall_of_text(client, publisher, tmp_path) -> None:
    publisher.send("interpreter", "owner", "update.shared", {"text": "x" * 1200})
    _sentinel(client, tmp_path).run_forever(block_ms=1, max_cycles=1)
    (correction,) = _corrections(client)
    assert correction.payload["rule_id"] == "chat.wall-of-text"
    assert correction.to_role == "interpreter"


def test_the_sentinel_corrects_a_blocker_the_owner_cannot_answer(client, publisher, tmp_path) -> None:
    publisher.send("interpreter", "owner", "questions.asked",
                   {"question_id": "q-01J5AB3CDEF4GH5JK6MN7PQ8RS",
                    "questions": [{"text": "The test_design gate keeps failing on I1.S3.B1. Thoughts?"}]})
    _sentinel(client, tmp_path).run_forever(block_ms=1, max_cycles=1)
    (correction,) = _corrections(client)
    assert correction.payload["rule_id"] == "chat.no-options"
    assert "2-4 options" in correction.payload["note"]


def test_a_short_message_with_options_draws_nothing(client, publisher, tmp_path) -> None:
    publisher.send("interpreter", "owner", "update.shared",
                   {"text": "Story I1.S2 is done. Try it: open index.html and place a mark."})
    publisher.send("interpreter", "owner", "questions.asked",
                   {"question_id": "q-01J5AB3CDEF4GH5JK6MN7PQ8RS",
                    "questions": [{"text": "Behaviour I1.S3.B1 is stuck on test quality.",
                                   "options": ["Let the specifier rewrite the test",
                                               "Drop the behaviour from this iteration"],
                                   "recommended": "Let the specifier rewrite the test"}]})
    _sentinel(client, tmp_path).run_forever(block_ms=1, max_cycles=1)
    assert _corrections(client) == []
