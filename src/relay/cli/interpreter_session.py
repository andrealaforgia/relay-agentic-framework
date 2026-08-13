"""relay chat — a live Claude session that IS the Interpreter.

One persistent `claude` process (stream-json in/out, resumable session):
  - your lines stream in as user messages; its replies stream OUT live;
  - bus events addressed to the interpreter (analyst questions, coordinator
    checkpoints) are fed into the same conversation, so it tells you about
    them in its own words the moment they arrive;
  - its formal moves still go through relay-send onto the audited ledger;
  - every owner utterance is recorded on the ledger before the model sees it.

This is NOT v1's keystroke injection: stdin stream-json is a supported
programmatic interface, and misdelivery is unrepresentable.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from ulid import ULID

from relay.bus import groups
from relay.bus.client import get_client
from relay.bus.keys import group_name, ledger_key
from relay.bus.publisher import Publisher
from relay.contract import ContractValidator, load_contract
from relay.contract.envelope import Envelope
from relay.contract.errors import ContractError

console = Console()

SESSION_PLAYBOOK_SUFFIX = """

== Session mode ==
You are LIVE with the Owner in this terminal. Speak to them directly in plain
text — that is your voice. Everything for other assistants or the coordinator
still goes through relay-send (your text is not work product for them).
Messages tagged [BUS EVENT] are incoming relay traffic delivered into this
conversation: react to them — tell the Owner what they mean in domain terms,
and reply on the bus with relay-send where the protocol requires it.
Messages tagged [OWNER] are the human typing. Their formal record (problem,
decisions, feedback) is already on the ledger — just respond and act.
Be concise. Never leave the Owner without a response.
"""

HELP = """\
Plain text goes to the Interpreter (and onto the ledger as your record).
  /decide <approve|reject> [comment]   decide the pending gate
  /status                              swarm liveness + current activity
  /help    /quit
"""


class InterpreterSession:
    def __init__(self, swarm: str, project: Path, model: str | None, settings: Path | None,
                 playbook_path: Path, state_dir: Path) -> None:
        self.swarm = swarm
        self.project = project
        self.model = model
        self.settings = settings
        self.playbook = playbook_path.read_text() + SESSION_PLAYBOOK_SUFFIX
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.session_file = state_dir / "session.txt"

        self.client = get_client()
        self.validator = ContractValidator(load_contract())
        self.publisher = Publisher(self.client, self.validator, swarm)
        self.stream = ledger_key(swarm)

        self.proc: subprocess.Popen[str] | None = None
        self.outbox: queue.Queue[str] = queue.Queue()
        self.turn_busy = threading.Event()
        self.problem_stated = False
        self.pending_gate_id: str | None = None
        self._stop = threading.Event()
        self._stdin_lock = threading.Lock()
        self._streamed_any = False
        self._turn_streamed = False
        self._warned_foreign = False

    # ── claude process ───────────────────────────────────────────────────────

    def _spawn(self) -> None:
        cmd = ["claude", "-p",
               "--input-format", "stream-json",
               "--output-format", "stream-json",
               "--include-partial-messages", "--verbose"]
        if self.model:
            cmd += ["--model", self.model]
        if self.settings:
            cmd += ["--settings", str(self.settings)]
        resume = self.session_file.read_text().strip() if self.session_file.exists() else ""
        if resume:
            cmd += ["--resume", resume]
        self.proc = subprocess.Popen(
            cmd, cwd=self.project, text=True, bufsize=1,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

    def _write_user_message(self, text: str) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        message = {"type": "user", "message": {"role": "user", "content": text}}
        with self._stdin_lock:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()

    def _feed(self, text: str) -> None:
        """Queue a user message; delivered when the current turn (if any) ends."""
        self.outbox.put(text)
        self._maybe_deliver()

    def _maybe_deliver(self) -> None:
        if self.turn_busy.is_set():
            return
        try:
            text = self.outbox.get_nowait()
        except queue.Empty:
            return
        self.turn_busy.set()
        self._write_user_message(text)

    # ── output rendering (streams live) ─────────────────────────────────────

    def _stdout_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                obj: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._render_stream_obj(obj)
        if not self._stop.is_set():
            console.print("\n[red]the interpreter session ended unexpectedly — "
                          "restart with `relay chat`[/red]")
            self._stop.set()

    def _render_stream_obj(self, obj: dict[str, Any]) -> None:
        kind = obj.get("type")
        if kind == "system" and obj.get("subtype") == "init":
            session_id = str(obj.get("session_id") or "")
            if session_id:
                self.session_file.write_text(session_id)
        elif kind == "stream_event":
            event = obj.get("event") or {}
            if event.get("type") == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    if not self._streamed_any:
                        console.print("[bold cyan]interpreter[/bold cyan] ", end="")
                        self._streamed_any = True
                    self._turn_streamed = True
                    print(delta.get("text", ""), end="", flush=True)
            elif event.get("type") == "content_block_start":
                block = event.get("content_block") or {}
                if block.get("type") == "tool_use":
                    if self._streamed_any:
                        print(flush=True)
                        self._streamed_any = False
                    console.print(f"[dim]  · {block.get('name', 'tool')}…[/dim]")
        elif kind == "assistant" and not self._turn_streamed:
            # fallback for claude builds that don't emit partial deltas
            for block in (obj.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    console.print(f"[bold cyan]interpreter[/bold cyan] {block['text'].strip()}")
        elif kind == "result":
            if self._streamed_any:
                print(flush=True)
                self._streamed_any = False
            self._turn_streamed = False
            self.turn_busy.clear()
            self._maybe_deliver()

    # ── bus consumption ──────────────────────────────────────────────────────

    def _bus_loop(self) -> None:
        from relay.bus import claims

        interp_group = group_name("interpreter")
        owner_group = group_name("owner")
        groups.ensure_group(self.client, self.stream, interp_group)
        groups.ensure_group(self.client, self.stream, owner_group)
        consumer = "interpreter-session"
        for group in (interp_group, owner_group):
            for delivery in groups.read_pending(self.client, self.stream, group, consumer):
                self._consume(group, delivery)
            # inherit anything a dead headless interpreter worker left pending
            for delivery in claims.autoclaim_stale(
                self.client, self.stream, group, consumer, min_idle_ms=60_000
            ):
                self._consume(group, delivery)
        while not self._stop.is_set():
            for group in (interp_group, owner_group):
                for delivery in groups.read_new(
                    self.client, self.stream, group, consumer, block_ms=500
                ):
                    self._consume(group, delivery)

    def _consume(self, group: str, delivery: groups.Delivery) -> None:
        env = delivery.envelope
        if env is None:
            if not self._warned_foreign:
                self._warned_foreign = True
                console.print("[dim]· skipping entries from another writer on this stream "
                              "(old swarm with the same name?) — see `relay status` dlq[/dim]")
            groups.ack(self.client, self.stream, group, delivery.stream_id)
            return
        if group == group_name("interpreter") and env.to_role == "interpreter" \
                and env.from_role != "owner":
            # feed relay traffic into the conversation; the model reacts live
            self._feed(
                f"[BUS EVENT] from={env.from_role} type={env.type} event_id={env.event_id}\n"
                f"payload: {json.dumps(env.payload, sort_keys=True)}\n"
                f"(reply on the bus with relay-send --reply-to {env.event_id} "
                f"where the protocol requires it, and brief the Owner in text)"
            )
        elif group == group_name("owner") and env.to_role == "owner" \
                and env.from_role != "interpreter":
            if env.type == "chat.progress":
                p = env.payload
                console.print(f"[dim]progress {p['iteration_id']}: "
                              f"{p['behaviours_done']}/{p['behaviours_total']} behaviours[/dim]")
        elif env.plane not in ("system",) and env.from_role not in ("owner", "interpreter"):
            ref = env.behaviour_id or env.story_id or env.iteration_id or ""
            console.print(f"[dim]· {env.from_role} → {env.to_role} · {env.type}"
                          f"{f' [{ref}]' if ref else ''}[/dim]")
        groups.ack(self.client, self.stream, group, delivery.stream_id)

    # ── owner input ──────────────────────────────────────────────────────────

    def _record_owner_line(self, line: str) -> None:
        """The ledger record of the human's words — written BEFORE the model acts."""
        try:
            if not self.problem_stated:
                self.publisher.send("owner", "interpreter", "chat.problem", {"text": line})
                self.problem_stated = True
            else:
                self.publisher.send("owner", "interpreter", "chat.feedback", {"text": line})
        except ContractError as e:
            console.print(f"[red]{e}[/red]")

    def dispatch_line(self, line: str) -> bool:
        line = line.strip()
        if not line:
            return True
        if line in ("/quit", "/exit"):
            return False
        if line == "/help":
            console.print(HELP)
            return True
        if line == "/status":
            self._print_status()
            return True
        if line.startswith("/decide"):
            parts = line.split(maxsplit=2)
            if len(parts) < 2 or parts[1] not in ("approve", "reject"):
                console.print("[red]usage: /decide approve|reject [comment][/red]")
                return True
            gate_id = self.pending_gate_id or f"gate-{ULID()}"
            payload: dict[str, object] = {"gate_id": gate_id, "decision": parts[1]}
            if len(parts) == 3:
                payload["comment"] = parts[2]
            try:
                self.publisher.send("owner", "interpreter", "chat.decision", payload)
            except ContractError as e:
                console.print(f"[red]{e}[/red]")
                return True
            self.pending_gate_id = None
            self._feed(f"[OWNER] decision on gate {gate_id}: {parts[1]}"
                       + (f" — {parts[2]}" if len(parts) == 3 else "")
                       + ". Act on it now (relay-send where required).")
            return True

        self._record_owner_line(line)
        self._feed(f"[OWNER] {line}")
        return True

    def _print_status(self) -> None:
        from relay.bus.keys import presence_key
        rows = []
        for key in sorted(str(k) for k in self.client.scan_iter(
                match=presence_key(self.swarm, "*", "*"))):
            raw = self.client.get(key)
            entry = key.rsplit(":", 1)[-1].split("@")[0]
            try:
                info = json.loads(str(raw))
                elapsed = int(time.time() - float(info.get("since", time.time())))
                rows.append(f"  {entry:<12} {info.get('status', '?')} ({elapsed}s)")
            except (json.JSONDecodeError, TypeError):
                rows.append(f"  {entry:<12} alive")
        console.print("\n".join(rows) if rows else "[red]no live workers[/red]")

    # ── main ─────────────────────────────────────────────────────────────────

    def run(self) -> None:
        console.print(f"[bold]relay chat[/bold] — swarm '{self.swarm}' · a live Claude session. "
                      f"/help for commands.")
        self._spawn()
        # bootstrap turn: the playbook is the first user message of the session
        # (skipped on resume — the session already has it)
        if not (self.session_file.exists() and self.session_file.read_text().strip()):
            self.turn_busy.set()
            self._write_user_message(
                self.playbook + "\n\nIntroduce yourself to the Owner in two sentences "
                "and ask for their problem."
            )
        threading.Thread(target=self._stdout_loop, daemon=True).start()
        threading.Thread(target=self._bus_loop, daemon=True).start()
        try:
            while not self._stop.is_set():
                line = console.input("[bold green]owner>[/bold green] ")
                if not self.dispatch_line(line):
                    break
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            self._stop.set()
            if self.proc is not None:
                if self.proc.stdin is not None:
                    self.proc.stdin.close()
                self.proc.terminate()
