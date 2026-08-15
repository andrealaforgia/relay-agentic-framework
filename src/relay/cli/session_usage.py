"""What the Interpreter's own session spent, read from its transcript.

Every other role runs inside a worker loop that knows what its turn cost. The
Interpreter is a native Claude Code session, so its spend is only visible in
the transcript Claude Code writes. The Stop hook reads the lines added since
it last looked and publishes them, which keeps the one unbounded, opus-priced
session from being the one nobody can see.

Read-only and forgiving by construction: a missing, truncated or half-written
transcript yields nothing rather than raising. Bookkeeping never breaks the
Owner's conversation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens", "output_tokens")


@dataclass
class TranscriptSlice:
    usage: dict[str, int] = field(default_factory=dict)
    model: str | None = None
    assistant_messages: int = 0
    lines_read: int = 0
    fresh: bool = True
    session_turn: int = 1


def _state(path: Path) -> dict[str, object]:
    try:
        return dict(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def read_new_usage(transcript: Path, state_path: Path) -> TranscriptSlice | None:
    """Usage recorded in `transcript` since the last `record_usage`, or None.

    A different transcript path means a different session: start from its
    first line rather than from an offset that means nothing there.
    """
    state = _state(state_path)
    same_session = str(state.get("transcript") or "") == str(transcript)
    start = int(str(state.get("lines") or 0)) if same_session else 0

    try:
        with transcript.open() as fh:
            lines = fh.readlines()
    except OSError:
        return None

    totals: dict[str, int] = {}
    per_model: dict[str, int] = {}
    seen = 0
    for line in lines[start:]:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # a half-written last line, or a format we do not know
        if obj.get("type") != "assistant":
            continue
        message = obj.get("message") or {}
        usage = message.get("usage") or {}
        if not usage:
            continue
        seen += 1
        for key in USAGE_KEYS:
            value = usage.get(key)
            if value is not None:
                totals[key] = totals.get(key, 0) + int(value)
        model = message.get("model")
        if model:
            # label the batch with the model that moved the bill most
            per_model[str(model)] = per_model.get(str(model), 0) + int(
                usage.get("output_tokens") or 0
            ) + int(usage.get("cache_creation_input_tokens") or 0)

    if seen == 0:
        return None
    return TranscriptSlice(
        usage=totals,
        model=max(per_model, key=lambda m: per_model[m]) if per_model else None,
        assistant_messages=seen,
        lines_read=len(lines),
        fresh=not same_session or start == 0,
        session_turn=int(str(state.get("turn") or 0)) + 1 if same_session else 1,
    )


def record_usage(state_path: Path, transcript: Path, slice_: TranscriptSlice | None) -> None:
    """Remember how far we read, so the next hook reports only what is new."""
    if slice_ is None:
        return
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "transcript": str(transcript),
        "lines": slice_.lines_read,
        "turn": slice_.session_turn,
    }))
