"""The ubiquitous language of the roadmap, enforced in code.

Work has exactly three units — Iteration, Story, Behaviour — and the contract
already validates their ids. What the contract cannot see is the prose: a
roadmap narrative that promises "a first round of basics", a checkpoint that
mentions "phase two". The words drift even when the structure holds, and the
drift reaches the Owner.

Detection is deliberately narrow. A banned word only counts when it is used
as a COUNTABLE UNIT OF WORK ("the first round", "three phases", "phase 2"),
because the same words are ordinary domain vocabulary — a card game has
rounds, a washing machine has phases, a runner has milestones. The sentinel's
own test applies: did the sender choose the word, or did the problem dictate
it? Counting is what betrays an invented unit of work.
"""

from __future__ import annotations

import re
from typing import Any

# the only units that exist, with the id shape the contract validates
UNITS = {
    "iteration": "I1",
    "story": "I1.S1",
    "behaviour": "I1.S1.B1",
}

NON_CONTRACT_UNITS = (
    "round", "sprint", "phase", "milestone", "step", "task",
    "chunk", "batch", "wave", "stage", "epic",
)

_QUANTIFIER = (
    r"first|second|third|fourth|fifth|next|last|final|another|each|every|"
    r"this|that|these|those|one|two|three|four|five|several|few|\d+"
)
_TERMS = "|".join(NON_CONTRACT_UNITS)
# "the first round", "three phases"  |  "round 2", "phase one"
_COUNTED_UNIT = re.compile(
    rf"\b(?:{_QUANTIFIER})\s+(?:few\s+)?(?:{_TERMS})s?\b"
    rf"|\b(?:{_TERMS})\s+(?:\d+|one|two|three|four|five)\b",
    re.IGNORECASE,
)


def counted_units(text: str) -> list[str]:
    """Phrases in `text` that name a unit of work the contract does not have."""
    return [" ".join(match.group(0).split()) for match in _COUNTED_UNIT.finditer(text)]


def scan_payload(payload: Any) -> list[str]:
    """Every offending phrase in every string the payload carries, nested."""
    found: list[str] = []
    if isinstance(payload, str):
        found.extend(counted_units(payload))
    elif isinstance(payload, dict):
        for value in payload.values():
            found.extend(scan_payload(value))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(scan_payload(item))
    # stable, de-duplicated, order preserved
    seen: dict[str, None] = {}
    for phrase in found:
        seen.setdefault(phrase.lower(), None)
    return list(seen)


def correction_note(phrases: list[str]) -> str:
    """What the culprit is told: what was said, and what to say instead."""
    quoted = ", ".join(f'"{p}"' for p in phrases[:4])
    return (
        f"{quoted} names a unit of work that does not exist. Work is "
        f"Iteration ({UNITS['iteration']}) > Story ({UNITS['story']}) > "
        f"Behaviour ({UNITS['behaviour']}) — use those words, with the id. "
        f"If this phrase is the Owner's own domain vocabulary rather than a "
        f"unit of work, acknowledge and carry on."
    )
