"""Waking an idle Interpreter session.

A Claude Code session only looks at its mail at its own turn boundaries or
when the Owner types. Nothing can wake it from outside: its control socket
answers nothing, and macOS refuses TIOCSTI on a terminal you do not own. The
one thing that works is holding the session's input yourself, which relay can
do because relay starts the session (`pty_proxy`).

This module is the decision half — pure functions, no terminals — so the
policy is testable and the byte pump stays thin.

What we type is a NUDGE, never the mail. Delivery still goes through
relay-inbox, so the ack, the ledger record and the audit are untouched, and a
nudge that misses costs nothing: the mail is still queued for the next
keystroke or the next Stop hook. v1 typed the payload into a window it did not
own and read the screen to decide when — this reads the ledger to decide when,
and carries nothing.
"""

from __future__ import annotations

from typing import Any

import redis

from relay.bus.keys import group_name, ledger_key

# don't type over the Owner mid-sentence, and don't ring twice in a row
QUIET_BEFORE_NUDGE_S = 1.5
NUDGE_COOLDOWN_S = 45.0


def nudge_text(swarm: str) -> str:
    """One line, synthetic, content-free. The leading '<' is the framework's
    existing marker for input that is not the Owner's words, so the
    UserPromptSubmit hook does not record it on the ledger."""
    return (f"<relay-wake> Relay mail arrived. Run `relay-inbox --swarm {swarm}` "
            f"now and act on what it gives you.")


def undelivered_for_interpreter(client: redis.Redis, swarm: str) -> list[str]:
    """Event ids addressed to the Interpreter that its consumer group has not
    read yet. Read-only: we never consume, so relay-inbox keeps ownership of
    the ack."""
    stream = ledger_key(swarm)
    try:
        groups = client.xinfo_groups(stream)
    except redis.RedisError:
        return []
    last = next((str(g.get("last-delivered-id"))
                 for g in groups if str(g.get("name")) == group_name("interpreter")), None)
    if last is None:
        return []
    try:
        entries: Any = client.xrange(stream, min=f"({last}", max="+", count=200)
    except redis.RedisError:
        return []
    waiting = []
    for _sid, fields in entries:
        to, frm = fields.get("to"), fields.get("from")
        if (to == "interpreter" and frm != "owner") or (
            to == "owner" and frm not in ("interpreter", "owner")
        ):
            waiting.append(fields.get("event_id", ""))
    return waiting


def should_nudge(
    *, waiting: list[str], quiet_for_s: float, since_last_nudge_s: float
) -> bool:
    """Ring only when there is mail, the Owner is not mid-sentence, and we did
    not just ring."""
    return bool(waiting) and quiet_for_s >= QUIET_BEFORE_NUDGE_S \
        and since_last_nudge_s >= NUDGE_COOLDOWN_S
