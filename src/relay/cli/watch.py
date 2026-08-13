"""relay watch — mission control in one terminal.

Top: per-assistant liveness and live activity (what each one is doing right
now, and for how long), plus the behaviour board computed with the same
projection the coordinator uses. Bottom: one merged feed — ledger events
(bold) interleaved with every worker's streamed activity (dim, role-colored):
each tool call, each turn, as it happens. Read-only: watching a swarm can
never affect it.
"""

from __future__ import annotations

import json as _json
import time
from collections import deque
from pathlib import Path
from typing import cast

import redis
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from relay.bus.client import get_client
from relay.bus.keys import ledger_key, presence_key
from relay.cli import procs
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
FEED_DEPTH = 26


class LogTails:
    """Incrementally follow every worker log; yields (role, line) as written."""

    def __init__(self, swarm: str) -> None:
        self.swarm = swarm
        self.offsets: dict[Path, int] = {}

    def read_new(self) -> list[tuple[str, str]]:
        lines: list[tuple[str, str]] = []
        log_dir = procs.log_dir(self.swarm)
        if not log_dir.is_dir():
            return lines
        for log in sorted(log_dir.glob("*.log")):
            size = log.stat().st_size
            if log not in self.offsets:
                self.offsets[log] = size  # attach at the end: live from now on
                continue
            if size < self.offsets[log]:
                self.offsets[log] = 0  # truncated/rotated
            if size == self.offsets[log]:
                continue
            with log.open() as f:
                f.seek(self.offsets[log])
                chunk = f.read()
                self.offsets[log] = f.tell()
            for line in chunk.splitlines():
                line = line.strip()
                if line:
                    lines.append((log.stem, line))
        return lines


def _event_line(env: Envelope) -> Text:
    line = Text()
    line.append(f"{env.seq or '':>4} ", style="bright_black")
    line.append(env.from_role, style=f"bold {ROLE_COLORS.get(env.from_role, 'white')}")
    line.append(" → ", style="bold")
    line.append(env.to_role, style=f"bold {ROLE_COLORS.get(env.to_role, 'white')}")
    line.append(f"  {env.type}", style="bold")
    ref = env.behaviour_id or env.story_id or env.iteration_id or ""
    if ref:
        line.append(f"  [{ref}]", style="bright_black")
    return line


def _activity_line(role: str, text: str) -> Text:
    line = Text()
    line.append("     ")
    line.append(f"{role} ▏", style=ROLE_COLORS.get(role, "white"))
    line.append(f" {text[:150]}", style="dim")
    return line


def _board(state: SwarmState) -> Table:
    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("behaviour", no_wrap=True)
    table.add_column("summary", ratio=3, no_wrap=True)
    table.add_column("state", ratio=1, no_wrap=True)
    table.add_column("attempt", justify="right")
    for bid in state.behaviour_order:
        b = state.behaviours[bid]
        icon, colour = STATE_ICONS.get(b.state, ("◌", "yellow"))
        blink = "blink " if b.state not in STATE_ICONS else ""
        summary = " ".join(b.ac_text.split())  # one line, whatever the AC's formatting
        if b.state == BehaviourState.AT_RED and b.last_fail_reason:
            summary = f"⚠ {b.last_fail_reason}"
        table.add_row(
            bid,
            Text(summary, style="dim" if b.state in STATE_ICONS else ""),
            Text(f"{icon} {b.state}", style=f"{blink}{colour}"),
            str(b.attempt) if b.attempt > 1 else "",
        )
    return table


def _presence(client: redis.Redis, swarm: str) -> Table:
    """Per-assistant liveness AND live activity — the 'is it stuck?' answer."""
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("assistant", width=14)
    table.add_column("activity", ratio=1)
    keys = sorted(str(k) for k in client.scan_iter(match=presence_key(swarm, "*", "*")))
    if not keys:
        table.add_row(Text("no live workers", style="red"), "")
        return table
    for key in keys:
        entry = key.rsplit(":", 1)[-1]
        role = entry.split("@")[0]
        raw = client.get(key)
        status, elapsed = "alive", ""
        try:
            info = _json.loads(str(raw))
            if isinstance(info, dict):  # older writers stored a bare pid
                status = str(info.get("status", "alive"))
                elapsed = f" ({int(time.time() - float(info.get('since', time.time())))}s)"
        except (ValueError, TypeError):
            pass
        style = "bright_black" if status == "idle" else "bold yellow"
        table.add_row(
            Text(role, style=ROLE_COLORS.get(role, "white")),
            Text(f"{status}{elapsed}", style=style),
        )
    return table


def watch(swarm: str, refresh_s: float = 0.5, cycles: int | None = None) -> None:
    client = get_client()
    console = Console()
    feed: deque[Text] = deque(maxlen=FEED_DEPTH)
    seen = 0
    state = SwarmState()
    tails = LogTails(swarm)

    with Live(console=console, refresh_per_second=4) as live:
        n = 0
        while cycles is None or n < cycles:
            entries = cast(
                "list[tuple[str, dict[str, str]]]", client.xrange(ledger_key(swarm))
            )
            new_events = []
            for _sid, fields in entries[seen:]:
                env = Envelope.try_from_fields(fields)
                if env is None:
                    continue  # foreign writer on this stream — the audit reports these
                apply(state, env)
                new_events.append((_sid, _event_line(env)))
            seen = len(entries)

            # interleave: worker activity streams in between the ledger events
            for role, raw_line in tails.read_new():
                feed.append(_activity_line(role, raw_line))
            for _sid, event_text in new_events:
                feed.append(event_text)

            live.update(Group(_presence(client, swarm), _board(state), *feed))
            time.sleep(refresh_s)
            n += 1
