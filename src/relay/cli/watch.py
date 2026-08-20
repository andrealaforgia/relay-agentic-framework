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
from relay.coordinator.model import Behaviour, BehaviourState, SwarmState
from relay.coordinator.projection import apply
from relay.ledger.usage import UsageFold, UsageReport, billed_input_equivalents

ROLE_COLORS = {
    "owner": "green", "interpreter": "cyan", "analyst": "blue",
    "specifier": "magenta", "builder": "yellow", "coordinator": "white",
    "toolgate": "bright_black", "reviewer": "red", "qa": "bright_magenta",
    "security": "bright_red", "curator": "green", "planner": "cyan",
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


def _blocked_why(state: SwarmState, behaviour_id: str) -> str | None:
    """The reason a blocked behaviour is blocked: the open Owner decision
    about it (that IS the blockage), else the last recorded failure."""
    for info in state.decisions.values():
        if info.subject_id == behaviour_id and not info.closed:
            return info.reason
    b = state.behaviours.get(behaviour_id)
    return b.last_fail_reason if b else None


def _board(state: SwarmState, max_rows: int | None = None) -> Table:
    """The behaviour board. A Live view cannot scroll, so when the roadmap
    outgrows the terminal (max_rows), finished stories collapse to one line
    each and the overflow to a count — active work always stays visible."""
    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("behaviour", no_wrap=True)
    table.add_column("summary", ratio=3, no_wrap=True)
    table.add_column("state", ratio=1, no_wrap=True)
    table.add_column("attempt", justify="right")

    behaviours = [state.behaviours[bid] for bid in state.behaviour_order]
    entries: list[tuple[str, object]] = [("beh", b) for b in behaviours]

    if max_rows is not None and len(entries) > max_rows:
        # a finished iteration becomes ONE ✓ line; then finished stories
        # (of unfinished iterations) one line each
        done_iterations = {
            iid for iid in {b.iteration_id for b in behaviours}
            if all(x.state is BehaviourState.DONE
                   for x in behaviours if x.iteration_id == iid)
        }
        emitted: set[str] = set()
        collapsed: list[tuple[str, object]] = []
        for b in behaviours:
            iid = b.iteration_id
            if iid in done_iterations:
                if iid not in emitted:
                    emitted.add(iid)
                    n = sum(1 for x in behaviours if x.iteration_id == iid)
                    collapsed.append(("story", (iid, n)))
                continue
            sid = b.story_id
            if sid and all(x.state is BehaviourState.DONE
                           for x in behaviours if x.story_id == sid):
                if sid not in emitted:
                    emitted.add(sid)
                    n = sum(1 for x in behaviours if x.story_id == sid)
                    collapsed.append(("story", (sid, n)))
                continue
            collapsed.append(("beh", b))
        entries = collapsed
        if len(entries) > max_rows:
            # keep every row that is in play; fill what's left in roadmap order
            budget = max(1, max_rows - 1)
            keep = [i for i, e in enumerate(entries)
                    if e[0] == "beh" and isinstance(e[1], Behaviour)
                    and e[1].state not in (BehaviourState.PLANNED, BehaviourState.DONE)]
            for i in range(len(entries)):
                if len(keep) >= budget:
                    break
                if i not in keep:
                    keep.append(i)
            kept = sorted(set(keep[:budget]))
            hidden = len(entries) - len(kept)
            entries = [entries[i] for i in kept]
            entries.append(("more", hidden))

    for kind, item in entries:
        if kind == "story":
            sid, n = cast("tuple[str, int]", item)
            table.add_row(sid, Text(f"all {n} behaviours done", style="dim"),
                          Text("✓ done", style="green"), "")
            continue
        if kind == "more":
            table.add_row("…", Text(f"{item} more not shown — `relay status` lists everything",
                                    style="dim"), "", "")
            continue
        b = cast("Behaviour", item)
        icon, colour = STATE_ICONS.get(b.state, ("◌", "yellow"))
        blink = "blink " if b.state not in STATE_ICONS else ""
        summary = goal_summary(b.title, b.ac_text)
        if b.state == BehaviourState.AT_RED and b.last_fail_reason:
            summary = f"⚠ {b.last_fail_reason}"
        if b.state == BehaviourState.BLOCKED:
            why = _blocked_why(state, b.id)
            if why:
                summary = f"✗ {why}"
        table.add_row(
            b.id,
            Text(summary, style="bold red" if b.state == BehaviourState.BLOCKED
                 else ("dim" if b.state in STATE_ICONS else "")),
            Text(f"{icon} {b.state}", style=f"{blink}{colour}"),
            str(b.attempt) if b.attempt > 1 else "",
        )
    return table


def _presence(client: redis.Redis, swarm: str) -> Table:
    """Per-assistant liveness AND live activity — the 'is it stuck?' answer.

    The roster is everything `relay up` started (pidfiles) plus everything
    heartbeating in Redis (workers on other machines) plus the native
    sessions. A worker that died must be a red row, never an absent one."""
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("assistant", width=14)
    table.add_column("activity", ratio=1)

    seen: set[str] = set()
    keys = sorted(str(k) for k in client.scan_iter(match=presence_key(swarm, "*", "*")))
    for key in keys:
        entry = key.rsplit(":", 1)[-1]
        role = entry.split("@")[0]
        seen.add(role)
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

    # started on this machine but not heartbeating: booting, or dead
    for pid_path in sorted(procs.run_dir(swarm).glob("*.pid")):
        role = pid_path.stem
        if role in seen:
            continue
        seen.add(role)
        try:
            alive = procs.is_running(int(pid_path.read_text().strip()))
        except (ValueError, OSError):
            alive = False
        table.add_row(
            Text(role, style=ROLE_COLORS.get(role, "white")),
            Text("starting…", style="yellow") if alive
            else Text("DOWN — `relay up` restarts it", style="bold red"),
        )
    if not seen:
        table.add_row(Text("no live workers", style="red"), Text("`relay up` starts them", style="bright_black"))

    # the native sessions have no worker process: their liveness is "does a
    # session exist, and is mail piling up for it?"
    from relay.cli.wake import undelivered_for_interpreter

    root = procs.state_root() / swarm
    if (root / "interpreter" / "native-session").exists():
        pending = len(undelivered_for_interpreter(client, swarm))
        detail = (Text(f"{pending} event(s) waiting — open `relay chat`", style="bold yellow")
                  if pending else Text("session (relay chat)", style="bright_black"))
    else:
        detail = Text("no session yet — open `relay chat`", style="bright_black")
    table.add_row(Text("interpreter", style=ROLE_COLORS["interpreter"]), detail)
    for role, cmd in (("curator", "relay learn"), ("planner", "relay plan")):
        if (root / role / "native-session").exists():
            table.add_row(Text(role, style=ROLE_COLORS.get(role, "white")),
                          Text(f"session ({cmd})", style="bright_black"))
    return table


PLANE_COLORS = {
    "chat": "green", "plan": "magenta", "work": "cyan", "gate": "dark_orange",
    "run": "blue", "control": "red", "system": "bright_black",
}


def _stamp(env: Envelope) -> str:
    from datetime import datetime

    try:
        return datetime.fromisoformat(env.ts).astimezone().strftime("%m-%d %H:%M:%S")
    except ValueError:
        return env.ts[:14]


def _payload_digest(env: Envelope) -> str:
    # the digest leads with what a human wants to read; ids shown in other
    # columns (or too noisy to matter) are dropped
    skip = {"contract_hash", "behaviour_id", "story_id", "iteration_id", "gate_id",
            "output_digest", "artifact_path", "test_paths"}
    # `fault` leads `exit_code` on purpose: it changes what the number means
    priority = ("text", "summary", "reason", "verdict", "decision", "questions",
                "answers", "fault", "exit_code", "cost_usd", "model", "how_to_try",
                "how_to_run", "pr_url", "problem", "detail", "goal")
    ordered = [k for k in priority if k in env.payload] + sorted(
        k for k in env.payload if k not in skip and k not in priority
    )
    parts = []
    for k in ordered:
        value = str(env.payload[k])
        if k == "commit_sha":
            value = value[:8]
        parts.append(f"{k}={value[:60]}")
    return " · ".join(parts)


def _is_system(env: Envelope) -> bool:
    """Telemetry and lifecycle: addressed to the system, not to a colleague."""
    return env.plane == "system" or env.to_role == "system"


def _event_row(env: Envelope) -> Text:
    stamp = _stamp(env)
    ref = env.behaviour_id or env.gate_id or env.story_id or env.iteration_id or ""
    detail = _payload_digest(env)
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
    """The ledger as a table, from the very first event, then live.

    Two lanes share the timeline: swarm events (assistants talking to each
    other) on the left, system-addressed events (telemetry, worker
    lifecycle) in their own right-hand column so the work reads clean."""
    client = get_client()
    console = Console()
    width = min(console.width or 170, 170)
    right_w = min(64, max(30, width // 3))
    left_w = width - right_w - 1

    def _pad_to(text: Text, cols: int) -> Text:
        text.truncate(cols)
        text.append(" " * max(0, cols - len(text.plain)))
        return text

    header = Text()
    header.append(f"{'seq':>5}  {'timestamp':<16}", style="bold")
    header.append(f"{'producer':>12}   {'recipient':<12}", style="bold")
    header.append(f"{'type':<24}{'ref':<12}detail", style="bold")
    _pad_to(header, left_w)
    header.append("┃ ", style="bright_black")
    header.append("→ system (telemetry & lifecycle)", style="bold")
    console.print(header)
    console.print("─" * left_w + "╂" + "─" * right_w, style="bright_black")

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
            if _is_system(env):
                # mirrors the left lane: origin → recipient, type, then the
                # payload digest in its own aligned column
                line = Text()
                line.append(f"{env.seq or '':>5}  ", style="bright_black")
                line.append(f"{_stamp(env)}  ", style="bright_black")
                _pad_to(line, left_w)
                line.append("┃ ", style="bright_black")
                line.append(f"{env.from_role:>10}",
                            style=ROLE_COLORS.get(env.from_role, "white"))
                line.append(" → ")
                line.append(f"{env.to_role:<7}",
                            style=ROLE_COLORS.get(env.to_role, "white"))
                line.append(f"{env.type:<22}", style="bright_black")
                line.append(_payload_digest(env), style="dim")
                line.truncate(width)
            else:
                line = _pad_to(_event_row(env), left_w)
                line.append("┃", style="bright_black")
            console.print(line)
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
    for column in ("model\nturns", "cold\nstarts", "agent\nloops",
                   "cache write\ntokens", "cache read\ntokens",
                   "output\ntokens", "cost\nUSD"):
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
    line.append("spend ", style="bright_black")
    line.append(f"${total['cost_usd']:.2f}", style="bold")
    if elapsed_s >= MIN_RATE_WINDOW_S and total["cost_usd"]:
        # a rate needs enough elapsed time to mean anything; before that,
        # showing one would just be a big number that frightens people
        line.append("   burn rate ", style="bright_black")
        line.append(f"${total['cost_usd'] / elapsed_s * 3600:.2f}/hour", style="bold yellow")
    line.append("   cache warmth ", style="bright_black")
    line.append(f"{warmth:.0%}", style="green" if warmth >= 0.8 else "yellow")
    line.append("   cold starts ", style="bright_black")
    line.append(f"{total['fresh_sessions']} of {total['turns']} turns")
    line.append("   billed-input equivalent ", style="bright_black")
    line.append(f"{billed_input_equivalents(total):,.0f} tokens")
    return line


def _tokens_legend() -> Text:
    """Every number on this screen, explained once, quietly."""
    rows = (
        ("model turns", "one model invocation per bus message handled"),
        ("cold starts", "turns with no resumed session: the whole context is re-read at full price"),
        ("agent loops", "tool-use cycles inside a turn — every loop re-sends the accumulated context"),
        ("cache write", "new context written to the prompt cache (billed ~1.25x the input rate)"),
        ("cache read", "context served from cache (billed ~0.1x — this discount is what 'warmth' measures)"),
        ("output", "tokens the model generated"),
        ("cost USD", "per-turn estimate from the runner's own usage report, summed"),
        ("billed-input equivalent", "all input normalised to full-price tokens — compare runs on this"),
    )
    legend = Text()
    for i, (term, meaning) in enumerate(rows):
        if i:
            legend.append("\n")
        legend.append(f"  {term:<24}", style="bright_black bold")
        legend.append(meaning, style="bright_black")
    return legend


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
    line.append(f"{int(payload.get('cache_read_input_tokens') or 0):>9,} tok from cache  ", style="dim")
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
                _tokens_legend(),
                Text(""),
                Text("last turns (newest at the bottom)", style="bold"),
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

            # a Live view cannot scroll: budget the board to what the
            # terminal can actually show, and let the feed take the rest
            term_h = console.size.height or 40
            presence_tbl = _presence(client, swarm)
            waiting = _waiting_panel(state)
            waiting_h = len(waiting.plain.splitlines()) if waiting.plain else 0
            feed_show = min(len(feed), max(6, term_h // 4))
            board_budget = max(6, term_h - presence_tbl.row_count
                               - waiting_h - feed_show - 5)
            live.update(Group(presence_tbl, _board(state, board_budget),
                              waiting, *list(feed)[-feed_show:]))
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
