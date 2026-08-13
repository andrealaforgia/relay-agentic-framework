"""relay-id — mint a fresh typed id (q-, gate-, run-, find-, or bare ULID).

Models are bad at inventing ULIDs; commands are good at it. Playbooks say:
    relay-id q      ->  q-01J5AB3CDEF4GH5JK6MN7PQ8RS
"""

from __future__ import annotations

import sys

from ulid import ULID

PREFIXES = {"q": "q-", "gate": "gate-", "run": "run-", "find": "find-", "event": ""}


def main() -> int:
    kind = sys.argv[1] if len(sys.argv) > 1 else "event"
    if kind not in PREFIXES:
        print(f"unknown id kind '{kind}' (choose from {', '.join(PREFIXES)})", file=sys.stderr)
        return 2
    print(f"{PREFIXES[kind]}{ULID()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
