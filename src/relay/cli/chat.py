"""The Owner's chat: async by design.

Input publishes immediately and returns; a background render thread shows
every message addressed to the owner as it arrives — replies, checkpoints,
progress — interleaved. (v1's owner chat blocked on exactly one reply; that
bug class is structurally gone here.)
"""

from __future__ import annotations

import json
import threading

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

HELP = """\
Plain text is sent with a context-inferred type (problem → answers → feedback).
Commands:
  /decide <approve|reject> [comment]   answer the pending gate (roadmap, checkpoint)
  /answer <text>                       answer the pending question explicitly
  /feedback <text>    /instruction <text>    /problem <text>
  /help               /quit
"""


class OwnerChat:
    def __init__(self, swarm: str) -> None:
        self.swarm = swarm
        self.client = get_client()
        self.publisher = Publisher(self.client, ContractValidator(load_contract()), swarm)
        self.stream = ledger_key(swarm)
        self.group = group_name("owner")
        self.consumer = "owner-chat"
        self.pending_question: Envelope | None = None
        self.pending_gate: Envelope | None = None
        self.problem_stated = False
        self._stop = threading.Event()

    # ── rendering ────────────────────────────────────────────────────────────

    def _render(self, env: Envelope) -> None:
        if env.type == "chat.question":
            self.pending_question = env
            console.print(f"\n[bold cyan]interpreter asks[/bold cyan] ({env.payload['question_id']}):")
            for q in env.payload["questions"]:
                console.print(f"  • {q['text']}")
                for opt in q.get("options", []):
                    marker = " [green](recommended)[/green]" if opt == q.get("recommended") else ""
                    console.print(f"      - {opt}{marker}")
        elif env.type == "chat.roadmap_proposed":
            self.pending_gate = env
            console.print("\n[bold magenta]roadmap proposed[/bold magenta] — /decide approve|reject")
            console.print(env.payload["narrative"])
            for it in env.payload["roadmap"]["iterations"]:
                console.print(f"  [bold]{it['id']}[/bold] {it['goal']} → {it['increment']}")
                for st in it["stories"]:
                    console.print(f"    {st['id']} {st['title']} ({len(st['acceptance_criteria'])} behaviours)")
        elif env.type == "chat.checkpoint":
            self.pending_gate = env
            console.print(f"\n[bold magenta]checkpoint[/bold magenta] {env.payload['kind']} "
                          f"{env.payload['subject_id']} — /decide approve|reject")
            console.print(env.payload["summary"])
        elif env.type == "chat.progress":
            p = env.payload
            console.print(f"[dim]progress {p['iteration_id']}: "
                          f"{p['behaviours_done']}/{p['behaviours_total']} behaviours[/dim]")
        else:
            body = env.payload.get("text") or env.payload.get("pr_url") or json.dumps(env.payload)
            console.print(f"\n[bold cyan]{env.from_role}[/bold cyan] ({env.type}): {body}")

    def _render_loop(self) -> None:
        groups.ensure_group(self.client, self.stream, self.group)
        for delivery in groups.read_pending(self.client, self.stream, self.group, self.consumer):
            self._consume(delivery)
        while not self._stop.is_set():
            for delivery in groups.read_new(
                self.client, self.stream, self.group, self.consumer, block_ms=1000
            ):
                self._consume(delivery)

    def _consume(self, delivery: groups.Delivery) -> None:
        env = delivery.envelope
        if env.to_role == "owner":
            self._render(env)
        elif env.plane not in ("system",) and env.from_role != "owner":
            # activity trace: the owner can always see the swarm working
            ref = f" [{env.behaviour_id or env.story_id or env.iteration_id}]" \
                if (env.behaviour_id or env.story_id or env.iteration_id) else ""
            console.print(f"[dim]· {env.from_role} → {env.to_role} · {env.type}{ref}[/dim]")
        groups.ack(self.client, self.stream, self.group, delivery.stream_id)

    # ── input ────────────────────────────────────────────────────────────────

    def _send(self, type_: str, payload: dict[str, object], reply_to: str | None = None) -> None:
        try:
            self.publisher.send("owner", "interpreter", type_, payload, in_reply_to=reply_to)
        except ContractError as e:
            console.print(f"[red]{e}[/red]")

    def dispatch_line(self, line: str) -> bool:
        """Returns False when the chat should exit."""
        line = line.strip()
        if not line:
            return True
        if line in ("/quit", "/exit"):
            return False
        if line == "/help":
            console.print(HELP)
            return True

        if line.startswith("/decide"):
            parts = line.split(maxsplit=2)
            if len(parts) < 2 or parts[1] not in ("approve", "reject"):
                console.print("[red]usage: /decide approve|reject [comment][/red]")
                return True
            if self.pending_gate is None:
                console.print("[red]nothing is awaiting a decision[/red]")
                return True
            payload = {"gate_id": self.pending_gate.payload["gate_id"], "decision": parts[1]}
            if len(parts) == 3:
                payload["comment"] = parts[2]
            self._send("chat.decision", payload, reply_to=self.pending_gate.event_id)
            self.pending_gate = None
            return True

        for prefix, type_ in (("/feedback", "chat.feedback"), ("/instruction", "chat.instruction"),
                              ("/problem", "chat.problem")):
            if line.startswith(prefix):
                text = line[len(prefix):].strip()
                self._send(type_, {"text": text})
                self.problem_stated = self.problem_stated or type_ == "chat.problem"
                return True

        if line.startswith("/answer"):
            line = line[len("/answer"):].strip()
            # falls through to the answer path below

        # contextual default: answer a pending question > state the problem > feedback
        if self.pending_question is not None:
            self._send(
                "chat.answer",
                {"question_id": self.pending_question.payload["question_id"], "answers": [line]},
                reply_to=self.pending_question.event_id,
            )
            self.pending_question = None
        elif not self.problem_stated:
            self._send("chat.problem", {"text": line})
            self.problem_stated = True
        else:
            self._send("chat.feedback", {"text": line})
        return True

    def run(self) -> None:
        console.print(f"[bold]relay chat[/bold] — swarm '{self.swarm}'. /help for commands.")
        thread = threading.Thread(target=self._render_loop, daemon=True)
        thread.start()
        try:
            while True:
                line = console.input("[bold green]owner>[/bold green] ")
                if not self.dispatch_line(line):
                    break
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            self._stop.set()


def _ulid() -> str:
    return str(ULID())
