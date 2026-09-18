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
    parser.add_argument(
        "--check", action="store_true",
        help="validate against the contract and publish nothing; never probe "
             "the ledger with a placeholder message to find a valid shape")
    args = parser.parse_args()

    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as e:
        print(f"payload is not valid JSON: {e}", file=sys.stderr)
        return 2

    validator = ContractValidator(load_contract())
    if args.check:
        try:
            validator.validate_payload(args.type_, payload)
        except ContractError as e:
            _explain(e, args.type_, payload)
            return 1
        print(f"valid '{args.type_}': nothing was published")
        return 0

    publisher = Publisher(get_client(), validator, args.swarm)
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
        _explain(e, args.type_, payload)
        return 1
    print(json.dumps({"event_id": result.event_id, "seq": result.seq, "stream_id": result.stream_id}))
    return 0


def _explain(error: ContractError, type_: str, payload: object) -> None:
    """Say what a valid message looks like, so the fix is in this turn and
    not in a filesystem search or a probe of the live ledger."""
    print(str(error), file=sys.stderr)
    from relay.contract.cheatsheet import required_fields

    needed = required_fields(load_contract(), type_)
    if needed:
        print(f"'{type_}' requires: {', '.join(needed)}", file=sys.stderr)
    if type_ == "behaviour.built" and "story_id" in str(error):
        behaviour = payload.get("behaviour_id") if isinstance(payload, dict) else None
        print(
            'an iteration-level behaviour (I<n>.INT) belongs to no story: send '
            '"story_id": null. A story\'s own behaviours, its I<n>.S<m>.INT '
            'included, send that story\'s id, e.g. "I<n>.S<m>".'
            + (f" This one is {behaviour}." if behaviour else ""),
            file=sys.stderr,
        )


if __name__ == "__main__":
    raise SystemExit(main())
