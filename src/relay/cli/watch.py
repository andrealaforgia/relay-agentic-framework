"""relay watch — the swarm in one terminal.

Top: per-assistant liveness and the iteration's behaviour board (done /
in-flight / pending / blocked), computed with the same projection the
coordinator uses. Bottom: the live event feed. Read-only: watching a swarm
can never affect it.
"""

from __future__ import annotations

import time
from collections import deque
from typing import cast

import redis
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from relay.bus.client import get_client
from relay.bus.keys import ledger_key, presence_key
from relay.contract.envelope import Envelope
from relay.coordinator.model import BehaviourState, SwarmState
from relay.coordinator.projection import apply

ROLE_COLORS = {
    "owner": "green", "interpreter": "cyan", "analyst": "blue",
    "specifier": "magenta", "builder": "yellow", "coordinator": "white",
    "toolgate": "bright_black", "reviewer": "red", "qa": "bright_magenta",
    "security": "bright_red", "sentinel": "bright_cyan",
}
STATE_ICONS = {
    BehaviourState.DONE: ("✓", "green"),
    BehaviourState.BLOCKED: ("✗", "red"),
    BehaviourState.PLANNED: ("·", "bright_black"),
}


def _feed_line(env: Envelope) -> Text:
    line = Text()
    line.append(f"{env.seq or '':>4} ", style="bright_black")
    line.append(env.from_role, style=ROLE_COLORS.get(env.from_role, "white"))
    line.append(" → ")
    line.append(env.to_role, style=ROLE_COLORS.get(env.to_role, "white"))
    line.append(f"  {env.type}", style="bold")
    ref = env.behaviour_id or env.story_id or env.iteration_id or ""
    if ref:
        line.append(f"  [{ref}]", style="bright_black")
    return line


def _board(state: SwarmState) -> Table:
    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("behaviour", ratio=2)
    table.add_column("state", ratio=1)
    table.add_column("attempt", justify="right")
    for bid in state.behaviour_order:
        b = state.behaviours[bid]
        icon, colour = STATE_ICONS.get(b.state, ("◌", "yellow"))
        blink = "blink " if b.state not in STATE_ICONS else ""
        table.add_row(
            bid,
            Text(f"{icon} {b.state}", style=f"{blink}{colour}"),
            str(b.attempt) if b.attempt > 1 else "",
        )
    return table


def _presence(client: redis.Redis, swarm: str) -> Text:
    live = sorted(
        str(k).rsplit(":", 1)[-1]
        for k in client.scan_iter(match=presence_key(swarm, "*", "*"))
    )
    text = Text("live: ")
    if not live:
        text.append("none", style="red")
    for i, entry in enumerate(live):
        role = entry.split("@")[0]
        if i:
            text.append("  ")
        text.append(entry, style=ROLE_COLORS.get(role, "white"))
    return text


def watch(swarm: str, refresh_s: float = 1.0, cycles: int | None = None) -> None:
    client = get_client()
    console = Console()
    feed: deque[Text] = deque(maxlen=18)
    seen = 0
    state = SwarmState()

    with Live(console=console, refresh_per_second=4) as live:
        n = 0
        while cycles is None or n < cycles:
            entries = cast(
                "list[tuple[str, dict[str, str]]]", client.xrange(ledger_key(swarm))
            )
            for _sid, fields in entries[seen:]:
                env = Envelope.from_fields(fields)
                apply(state, env)
                feed.append(_feed_line(env))
            seen = len(entries)
            live.update(Group(_presence(client, swarm), _board(state), *feed))
            time.sleep(refresh_s)
            n += 1
