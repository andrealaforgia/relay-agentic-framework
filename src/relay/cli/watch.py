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
from relay.ledger.usage import UsageFold, UsageReport, billed_input_equivalents

ROLE_COLORS = {
    "owner": "green", "interpreter": "cyan", "analyst": "blue",
    "specifier": "magenta", "builder": "yellow", "coordinator": "white",
    "toolgate": "bright_black", "reviewer": "red", "qa": "bright_magenta",
    "security": "bright_red",
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


def goal_summary(title: str, ac_text: str) -> str:
    """What the behaviour achieves, in one line: the title when given,
    otherwise the outcome ('then …') clause — never the Given preamble."""
    if title:
        return title
    import re

    text = " ".join(ac_text.split())
    for keyword in ("then", "when"):
        match = re.search(rf"\b{keyword}\b[,:]?\s+(.+)$", text, re.IGNORECASE)
        if match:
            outcome = match.group(1).strip().rstrip(".")
            return outcome[:1].upper() + outcome[1:]
    return text


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
        summary = goal_summary(b.title, b.ac_text)
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


PLANE_COLORS = {
    "chat": "green", "plan": "magenta", "work": "cyan", "gate": "dark_orange",
    "run": "blue", "control": "red", "system": "bright_black",
}


def _event_row(env: Envelope) -> Text:
    from datetime import datetime

    try:
        stamp = datetime.fromisoformat(env.ts).astimezone().strftime("%m-%d %H:%M:%S")
    except ValueError:
        stamp = env.ts[:14]
    ref = env.behaviour_id or env.gate_id or env.story_id or env.iteration_id or ""
    # the digest leads with what a human wants to read; ids shown in other
    # columns (or too noisy to matter) are dropped
    skip = {"contract_hash", "behaviour_id", "story_id", "iteration_id", "gate_id",
            "output_digest", "artifact_path", "test_paths"}
    priority = ("text", "summary", "reason", "verdict", "decision", "questions",
                "answers", "exit_code", "how_to_try", "how_to_run", "pr_url",
                "problem", "detail", "goal")
    ordered = [k for k in priority if k in env.payload] + sorted(
        k for k in env.payload if k not in skip and k not in priority
    )
    parts = []
    for k in ordered:
        value = str(env.payload[k])
        if k == "commit_sha":
            value = value[:8]
        parts.append(f"{k}={value[:60]}")
    detail = " · ".join(parts)
    row = Text()
    row.append(f"{env.seq or '':>5}  ", style="bright_black")
    row.append(f"{stamp}  ", style="bright_black")
    row.append(f"{env.from_role:>12}", style=ROLE_COLORS.get(env.from_role, "white"))
    row.append(" → ")
    row.append(f"{env.to_role:<12}", style=ROLE_COLORS.get(env.to_role, "white"))
    row.append(f"{env.type:<24}", style=f"bold {PLANE_COLORS.get(env.plane, 'white')}")
    row.append(f"{ref:<12}", style="bright_black")
    row.append(detail[:130], style="dim")
    return row


def events_view(swarm: str, follow: bool = True, refresh_s: float = 0.5,
                cycles: int | None = None) -> None:
    """The ledger as a table, from the very first event, then live."""
    client = get_client()
    console = Console()
    header = Text()
    header.append(f"{'seq':>5}  {'timestamp':<16}", style="bold")
    header.append(f"{'producer':>12}   {'recipient':<12}", style="bold")
    header.append(f"{'type':<24}{'ref':<12}detail", style="bold")
    console.print(header)
    console.print("─" * min(console.width, 140), style="bright_black")

    seen = 0
    n = 0
    while True:
        entries = cast(
            "list[tuple[str, dict[str, str]]]", client.xrange(ledger_key(swarm))
        )
        for _sid, fields in entries[seen:]:
            env = Envelope.try_from_fields(fields)
            if env is None:
                console.print(Text("       (entry from another writer — skipped)",
                                   style="dim red"))
                continue
            console.print(_event_row(env))
        seen = len(entries)
        n += 1
        if not follow or (cycles is not None and n >= cycles):
            return
        time.sleep(refresh_s)


# ── token burn, live ──────────────────────────────────────────────────────────

MIN_RATE_WINDOW_S = 60.0   # below this, a rate is noise dressed as a number
TOKEN_FEED_DEPTH = 12


def _short_model(model: str) -> str:
    """claude-sonnet-5 → sonnet: the tier is the part that changes the bill."""
    for tier in ("opus", "sonnet", "haiku", "fable"):
        if tier in model:
            return tier
    return model


def _tokens_table(report: UsageReport) -> Table:
    # sized to its content, never expanded: money must not be the column the
    # terminal decides to abbreviate
    table = Table(show_header=True, header_style="bold")
    table.add_column("role", no_wrap=True)
    table.add_column("model", no_wrap=True)
    for column in ("turns", "cold", "loops", "written", "read", "out", "cost"):
        table.add_column(column, justify="right", no_wrap=True)

    for role, row in sorted(report.by_role.items(), key=lambda kv: -float(kv[1]["cost_usd"])):
        models = sorted(_short_model(m) for m in row["models"])
        table.add_row(
            Text(role, style=ROLE_COLORS.get(role, "white")),
            Text(", ".join(models), style="bright_black"),
            f"{row['turns']:,}",
            Text(f"{row['fresh_sessions']:,}", style="yellow" if row["fresh_sessions"] else ""),
            f"{row['agent_turns']:,}",
            f"{row['cache_creation_input_tokens']:,}",
            f"{row['cache_read_input_tokens']:,}",
            f"{row['output_tokens']:,}",
            Text(f"${row['cost_usd']:.2f}", style="bold"),
        )
    total = report.total
    table.add_row(
        Text("total", style="bold"), "",
        Text(f"{total['turns']:,}", style="bold"),
        Text(f"{total['fresh_sessions']:,}", style="bold"),
        Text(f"{total['agent_turns']:,}", style="bold"),
        Text(f"{total['cache_creation_input_tokens']:,}", style="bold"),
        Text(f"{total['cache_read_input_tokens']:,}", style="bold"),
        Text(f"{total['output_tokens']:,}", style="bold"),
        Text(f"${total['cost_usd']:.2f}", style="bold"),
    )
    return table


def _tokens_summary(report: UsageReport, elapsed_s: float) -> Text:
    """Spend, burn rate, and the two numbers that explain them."""
    total = report.total
    written = int(total["cache_creation_input_tokens"])
    read = int(total["cache_read_input_tokens"])
    warmth = read / (read + written) if (read + written) else 0.0

    line = Text()
    line.append(f"${total['cost_usd']:.2f}", style="bold")
    if elapsed_s >= MIN_RATE_WINDOW_S and total["cost_usd"]:
        # a rate needs enough elapsed time to mean anything; before that,
        # showing one would just be a big number that frightens people
        line.append(f"  (${total['cost_usd'] / elapsed_s * 3600:.2f}/h)", style="bold yellow")
    line.append(f"  ·  {billed_input_equivalents(total):,.0f} billed input equivalents",
                style="bright_black")
    line.append(f"  ·  cache warmth {warmth:.0%}",
                style="green" if warmth >= 0.8 else "yellow")
    line.append(f"  ·  {total['fresh_sessions']} of {total['turns']} cold",
                style="bright_black")
    return line


def _usage_line(env: Envelope) -> Text:
    payload = env.payload
    ref = env.behaviour_id or env.gate_id or env.iteration_id or ""
    role = str(payload.get("role") or env.from_role)
    # one turn, one line: a wrapped feed reads as noise
    line = Text(no_wrap=True, overflow="ellipsis")
    line.append(f"{env.seq or '':>5}  ", style="bright_black")
    line.append(f"{role:<12}", style=ROLE_COLORS.get(role, "white"))
    line.append(f"{_short_model(str(payload.get('model', '?'))):<7}", style="bright_black")
    # the currency stays glued to the number; the column aligns around it
    line.append(f"{'$' + format(float(payload.get('cost_usd') or 0.0), '.2f'):>7}  ", style="bold")
    line.append(f"{int(payload.get('agent_turns') or 0):>3} loops  ", style="bright_black")
    line.append(f"{int(payload.get('cache_read_input_tokens') or 0):>9,} read  ", style="dim")
    line.append(f"{ref:<12}", style="bright_black")
    line.append(str(payload.get("trigger_type", "")), style="dim")
    if payload.get("fresh_session"):
        line.append("  cold", style="yellow")
    return line


def tokens_view(swarm: str, refresh_s: float = 0.5, cycles: int | None = None) -> None:
    """Token burn as it happens: the same fold `relay costs` prints, live."""
    client = get_client()
    console = Console()
    fold = UsageFold()
    feed: deque[Text] = deque(maxlen=TOKEN_FEED_DEPTH)
    started = time.monotonic()
    seen = 0

    with Live(console=console, refresh_per_second=4) as live:
        n = 0
        while cycles is None or n < cycles:
            entries = cast(
                "list[tuple[str, dict[str, str]]]", client.xrange(ledger_key(swarm))
            )
            for _sid, fields in entries[seen:]:
                env = Envelope.try_from_fields(fields)
                if env is not None and fold.add(env):
                    feed.append(_usage_line(env))
            seen = len(entries)

            report = fold.report()
            live.update(Group(
                _tokens_table(report),
                _tokens_summary(report, time.monotonic() - started),
                Text(""),
                *feed,
            ))
            time.sleep(refresh_s)
            n += 1


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

            live.update(Group(_presence(client, swarm), _board(state),
                              _waiting_panel(state), *feed))
            time.sleep(refresh_s)
            n += 1


def _waiting_panel(state: SwarmState) -> Text:
    """The 'is it stuck?' answer, always on screen. OWNER lines glow: when
    the swarm is waiting on the human, the human should not have to ask."""
    from relay.coordinator.diagnosis import render

    report = render(state, time.time())
    if not report:
        return Text("")
    text = Text()
    for i, line in enumerate(report.splitlines()):
        if i:
            text.append("\n")
        style = "bold" if i == 0 else ("bold yellow" if line.startswith("⚠") else "dim")
        text.append(line, style=style)
    text.append("\n")
    return text
