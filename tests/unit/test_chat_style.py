"""The Owner gets a few sentences, and never a blocker they cannot answer.

Both failures are silent: a wall of text is not an error, and an option-less
question validates against the contract perfectly well. The Owner just stops
answering — which is exactly what happened when three escalations sat
unanswered on a live run.

(The chat_style helpers are pure analysis; the enforcement that used them —
the sentinel's realm auditing — was removed 2026-08-17 pending a redesign,
and these rules should be re-wired by whatever replaces it.)
"""

from relay.chat_style import MAX_OWNER_TEXT, overlong, unanswerable_questions


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
