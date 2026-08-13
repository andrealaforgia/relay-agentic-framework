"""relay-inbox — the Interpreter's mail tap, for native Claude Code sessions.

The Interpreter IS a Claude Code session (`relay chat` execs the real
`claude`). This CLI is how relay traffic reaches that session:

  relay-inbox --swarm X                 drain pending bus messages (and ack)
  relay-inbox --swarm X --wait 240      block until mail arrives, then drain
  relay-inbox --swarm X --hook-stop     Stop hook: if mail is pending, block
                                        the stop and feed it as instruction
  relay-inbox --swarm X --hook-prompt   UserPromptSubmit hook: record the
                                        owner's words on the ledger, surface
                                        any queued mail as extra context

Messages are acked on drain: the Claude Code session transcript is the
continuation context from there on.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator

import redis

from relay.bus import claims, groups
from relay.bus.client import get_client
from relay.bus.keys import group_name, ledger_key
from relay.bus.publisher import Publisher
from relay.contract import ContractValidator, load_contract
from relay.contract.envelope import Envelope
from relay.contract.errors import ContractError

CONSUMER = "interpreter-native"


def _drain(client: redis.Redis, swarm: str, block_ms: int = 0) -> list[Envelope]:
    """Collect (and ack) everything addressed to the interpreter or the owner."""
    stream = ledger_key(swarm)
    collected: list[Envelope] = []
    for group in (group_name("interpreter"), group_name("owner")):
        groups.ensure_group(client, stream, group)
        deliveries = groups.read_pending(client, stream, group, CONSUMER)
        deliveries += claims.autoclaim_stale(client, stream, group, CONSUMER, min_idle_ms=60_000)
        deliveries += groups.read_new(client, stream, group, CONSUMER, block_ms=block_ms, count=64)
        for delivery in deliveries:
            env = delivery.envelope
            groups.ack(client, stream, group, delivery.stream_id)
            if env is None:
                continue  # foreign writer on this stream — quarantined elsewhere
            wanted = (
                (env.to_role == "interpreter" and env.from_role != "owner")
                or (env.to_role == "owner" and env.from_role not in ("interpreter", "owner"))
            )
            if wanted:
                collected.append(env)
        block_ms = 0  # only the first group read blocks
    return collected


def _render(messages: list[Envelope], swarm: str) -> str:
    if not messages:
        return ""
    lines = [f"== Relay mail ({len(messages)} message{'s' if len(messages) != 1 else ''}) =="]
    for env in messages:
        lines.append(
            f"[BUS EVENT] from={env.from_role} type={env.type} event_id={env.event_id}\n"
            f"payload: {json.dumps(env.payload, sort_keys=True)}"
        )
    lines.append(
        f"(Act on these now: brief the Owner in your reply, and answer on the bus with "
        f"relay-send --swarm {swarm} --from interpreter --reply-to <event_id> where the "
        f"protocol requires it.)"
    )
    return "\n\n".join(lines)


def _problem_already_stated(client: redis.Redis, swarm: str) -> bool:
    for _sid, env in _scan(client, swarm):
        if env.type == "problem.stated":
            return True
    return False


def _scan(client: redis.Redis, swarm: str) -> "Iterator[tuple[str, Envelope]]":
    from relay.ledger.reader import read_all

    return read_all(client, swarm)


def main() -> int:
    parser = argparse.ArgumentParser(prog="relay-inbox", description=__doc__)
    parser.add_argument("--swarm", required=True)
    parser.add_argument("--wait", type=int, default=0, metavar="SECONDS",
                        help="block up to SECONDS for mail before draining")
    parser.add_argument("--hook-stop", action="store_true")
    parser.add_argument("--hook-prompt", action="store_true")
    args = parser.parse_args()
    client = get_client()

    if args.hook_prompt:
        # stdin: Claude Code hook payload; record the owner's words, then
        # surface queued mail as additional context for this turn.
        try:
            payload = json.load(sys.stdin)
            prompt = str(payload.get("prompt", "")).strip()
        except (json.JSONDecodeError, OSError):
            prompt = ""
        if prompt and not prompt.startswith("/"):
            publisher = Publisher(client, ContractValidator(load_contract()), args.swarm)
            type_ = "feedback.given" if _problem_already_stated(client, args.swarm) else "problem.stated"
            try:
                publisher.send("owner", "interpreter", type_, {"text": prompt[:4000]})
            except ContractError:
                pass  # never break the owner's typing over a record-keeping hiccup
        rendered = _render(_drain(client, args.swarm), args.swarm)
        if rendered:
            print(rendered)
        return 0

    if args.hook_stop:
        # if mail is pending when the session tries to stop, keep it going
        messages = _drain(client, args.swarm)
        if messages:
            print(json.dumps({
                "decision": "block",
                "reason": _render(messages, args.swarm),
            }))
        return 0

    messages = _drain(client, args.swarm, block_ms=args.wait * 1000)
    rendered = _render(messages, args.swarm)
    print(rendered if rendered else "(no relay mail)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
