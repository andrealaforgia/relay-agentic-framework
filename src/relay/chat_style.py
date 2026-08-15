"""Two rules about talking to the Owner, checked in code.

The Interpreter's playbook asks for a few sentences and for every blocker to
arrive as a question with named options and a recommendation. Both are rules a
model has to remember, which D1 says we never rely on — and both fail quietly:
a wall of text is not an error, and a question with no options still validates
against the contract. The Owner simply stops answering.

The verbosity rule is cheap in a second way. `relay chat` is a resumed
session, so every sentence the Interpreter writes is re-read on every turn
that follows it.

Both checks are structural, not semantic: they count characters and look for
payload fields. No model turn is spent deciding either.
"""

from __future__ import annotations

from typing import Any

# about four sentences. The playbook says "a few"; this is where "a few" stops
# being a matter of opinion.
MAX_OWNER_TEXT = 700
LENGTH_CHECKED = {
    "update.shared": "text",
    "checkpoint.reached": "summary",
}


def overlong(type_: str, payload: dict[str, Any]) -> int:
    """Characters over the limit, or 0. The Owner reads this in a terminal."""
    field = LENGTH_CHECKED.get(type_)
    if field is None:
        return 0
    text = str(payload.get(field) or "")
    return max(0, len(text) - MAX_OWNER_TEXT)


def unanswerable_questions(payload: dict[str, Any]) -> list[str]:
    """Questions the Owner cannot answer in one line: no options to pick from,
    or options with nothing recommended."""
    questions = payload.get("questions")
    if not isinstance(questions, list):
        return []
    bad = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        options = q.get("options")
        if not isinstance(options, list) or len(options) < 2:
            bad.append(str(q.get("text", ""))[:80])
        elif not q.get("recommended"):
            bad.append(str(q.get("text", ""))[:80])
    return bad
