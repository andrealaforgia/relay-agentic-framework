"""relay-send — the ONLY output channel a model (or an operator) has.

The worker never parses model stdout; the model's work product is whatever it
publishes here, and this path enforces the full contract before anything
reaches the stream. A schema error is printed to stderr in-turn so the model
can self-correct and retry.
"""

from __future__ import annotations

import argparse
import json
import sys

from relay.bus.client import get_client
from relay.bus.publisher import Publisher
from relay.contract import ContractValidator, load_contract
from relay.contract.errors import ContractError


def main() -> int:
    parser = argparse.ArgumentParser(prog="relay-send", description=__doc__)
    parser.add_argument("--swarm", required=True)
    parser.add_argument("--from", dest="from_role", required=True)
    parser.add_argument("--to", dest="to_role", required=True)
    parser.add_argument("--type", dest="type_", required=True)
    parser.add_argument("--payload", required=True, help="JSON object")
    parser.add_argument("--reply-to", default=None, help="event_id this answers")
    parser.add_argument("--iteration", default=None)
    parser.add_argument("--story", default=None)
    parser.add_argument("--behaviour", default=None)
    parser.add_argument("--gate", default=None)
    parser.add_argument("--commit", default=None)
    args = parser.parse_args()

    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as e:
        print(f"payload is not valid JSON: {e}", file=sys.stderr)
        return 2

    publisher = Publisher(get_client(), ContractValidator(load_contract()), args.swarm)
    try:
        result = publisher.send(
            args.from_role,
            args.to_role,
            args.type_,
            payload,
            in_reply_to=args.reply_to,
            iteration_id=args.iteration,
            story_id=args.story,
            behaviour_id=args.behaviour,
            gate_id=args.gate,
            commit_sha=args.commit,
        )
    except ContractError as e:
        print(str(e), file=sys.stderr)
        # say what a valid one looks like, so the fix is in this turn and not
        # in a filesystem search
        from relay.contract.cheatsheet import required_fields

        needed = required_fields(load_contract(), args.type_)
        if needed:
            print(f"'{args.type_}' requires: {', '.join(needed)}", file=sys.stderr)
        return 1
    print(json.dumps({"event_id": result.event_id, "seq": result.seq, "stream_id": result.stream_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
