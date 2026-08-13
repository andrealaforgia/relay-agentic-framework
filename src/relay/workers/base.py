"""The worker lifecycle every role shares.

Startup order is always: drain own PEL -> XAUTOCLAIM stale entries from dead
consumers -> block on '>'. A message is acked only when handling produced a
verifiable result (subclasses define what that means); messages redelivered
past the delivery cap go to the DLQ — loudly, never silently.
"""

from __future__ import annotations

import json
import os
import socket
import time

import redis

from relay.bus import claims, dedup, dlq, groups
from relay.bus.client import get_client
from relay.bus.keys import group_name, ledger_key, presence_key
from relay.bus.publisher import Publisher
from relay.contract import ContractValidator, load_contract
from relay.contract.envelope import Envelope

DELIVERY_CAP = 5
PRESENCE_TTL_S = 45


class Worker:
    """Subclasses implement handle(envelope) -> str | None (result event id).

    Returning a result id (or having independently verified output on the
    stream) acks the trigger; raising keeps it pending for redelivery.
    """

    def __init__(self, swarm: str, role: str, client: redis.Redis | None = None) -> None:
        self.swarm = swarm
        self.role = role
        self.client = client if client is not None else get_client()
        self.validator = ContractValidator(load_contract())
        self.publisher = Publisher(self.client, self.validator, swarm)
        self.stream = ledger_key(swarm)
        self.group = group_name(role)
        self.consumer = f"{role}@{socket.gethostname()}#{os.getpid()}"
        self._stopping = False
        self._paused = False
        self._drain_pel_next_step = False
        # corrections received from the sentinel, surfaced to the next model turn
        self.pending_corrections: list[Envelope] = []

    # ── subclass surface ─────────────────────────────────────────────────────

    def handle(self, env: Envelope) -> str | None:
        raise NotImplementedError

    def wants(self, env: Envelope) -> bool:
        return env.to_role == self.role

    def on_tick(self) -> None:
        """Called after every read cycle (idle or not)."""

    # ── lifecycle ────────────────────────────────────────────────────────────

    def announce_started(self) -> None:
        self.publisher.send(
            self.role, "system", "worker.started",
            {
                "role": self.role,
                "host": socket.gethostname(),
                "pid": os.getpid(),
                "worker_version": _version(),
                "contract_hash": self.validator.contract.contract_hash,
            },
        )

    def announce_stopped(self) -> None:
        self.publisher.send(
            self.role, "system", "worker.stopped",
            {"role": self.role, "host": socket.gethostname(), "pid": os.getpid()},
        )

    def stop(self) -> None:
        self._stopping = True

    def heartbeat(self, status: str | None = None) -> None:
        if status is not None:
            self._status = status
            self._status_since = time.time()
        payload = json.dumps({
            "pid": os.getpid(),
            "status": getattr(self, "_status", "idle"),
            "since": getattr(self, "_status_since", time.time()),
        })
        self.client.set(
            presence_key(self.swarm, self.role, socket.gethostname()),
            payload,
            ex=PRESENCE_TTL_S,
        )

    def start(self) -> None:
        """Startup order, always: group -> announce -> drain PEL -> autoclaim."""
        groups.ensure_group(self.client, self.stream, self.group)
        self.announce_started()
        self.heartbeat()
        for delivery in groups.read_pending(self.client, self.stream, self.group, self.consumer):
            self._process(delivery)
        for delivery in claims.autoclaim_stale(self.client, self.stream, self.group, self.consumer):
            self._process(delivery)

    def step(self, block_ms: int = 5000) -> int:
        """One read cycle. Returns how many deliveries were processed."""
        if self._drain_pel_next_step and not self._paused:
            self._drain_pel_next_step = False
            for delivery in groups.read_pending(self.client, self.stream, self.group, self.consumer):
                self._process(delivery)
        deliveries = groups.read_new(
            self.client, self.stream, self.group, self.consumer, block_ms=block_ms
        )
        for delivery in deliveries:
            self._process(delivery)
        self.heartbeat()
        self.on_tick()
        return len(deliveries)

    def run_forever(self, block_ms: int = 5000, max_cycles: int | None = None) -> None:
        self.start()
        cycles = 0
        backoff = 1.0
        while not self._stopping:
            try:
                self.step(block_ms=block_ms)
                backoff = 1.0
            except redis.RedisError as e:
                # Redis down is failure mode #6: reconnect loudly, never die
                # silently. Unacked deliveries replay after recovery.
                print(f"!! redis error ({e}) — retrying in {backoff:.0f}s", flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
        self.announce_stopped()

    # ── per-delivery processing ──────────────────────────────────────────────

    def _process(self, delivery: groups.Delivery) -> None:
        env = delivery.envelope
        if env is None:
            # foreign/corrupt entry: skip and ack — the coordinator dead-letters it
            print(f"[{_now()}] skipping non-envelope entry {delivery.stream_id}", flush=True)
            groups.ack(self.client, self.stream, self.group, delivery.stream_id)
            return
        if env.to_role == self.role and env.plane == "control":
            self._handle_control(env)
            groups.ack(self.client, self.stream, self.group, delivery.stream_id)
            return
        if not self.wants(env):
            groups.ack(self.client, self.stream, self.group, delivery.stream_id)
            return
        if self._paused:
            # a paused role leaves work in the PEL: nothing is lost, nothing
            # is processed until resume.ordered
            return
        if dedup.already_done(self.client, self.swarm, self.role, env.event_id):
            groups.ack(self.client, self.stream, self.group, delivery.stream_id)
            return
        if claims.delivery_count(self.client, self.stream, self.group, delivery.stream_id) > DELIVERY_CAP:
            dlq.route_to_dlq(
                self.client, self.publisher, self.swarm, self.role,
                "delivery_cap_exceeded", env.to_fields(),
                f"gave up after {DELIVERY_CAP} deliveries",
            )
            groups.ack(self.client, self.stream, self.group, delivery.stream_id)
            return

        started = time.monotonic()
        print(f"[{_now()}] handling {env.type} from {env.from_role} ({env.event_id})", flush=True)
        self.heartbeat(status=f"working: {env.type}"
                       + (f" [{env.behaviour_id}]" if env.behaviour_id else ""))
        try:
            result_id = self.handle(env)
        finally:
            self.heartbeat(status="idle")
        outcome = f"reply {result_id}" if result_id is not None else "no result (see above)"
        print(f"[{_now()}] done in {time.monotonic() - started:.0f}s — {outcome}", flush=True)
        if result_id is not None:
            dedup.mark_done(self.client, self.swarm, self.role, env.event_id, result_id)
        groups.ack(self.client, self.stream, self.group, delivery.stream_id)


    def _handle_control(self, env: Envelope) -> None:
        """Control plane: deterministic worker duties, never the model's.

        The ack is published by the WORKER on receipt — whether the model's
        behaviour then changes is judged by the sentinel from later traffic.
        """
        if env.type == "correction.issued":
            print(f"[{_now()}] correction from sentinel: {env.payload.get('rule_id')}", flush=True)
            self.pending_corrections.append(env)
            self.publisher.send(
                self.role, "sentinel", "correction.acknowledged",
                {"finding_id": env.payload["finding_id"]},
                in_reply_to=env.event_id,
            )
        elif env.type == "pause.ordered":
            print(f"[{_now()}] PAUSED by coordinator: {env.payload.get('reason', '')}", flush=True)
            self._paused = True
            self.heartbeat(status="paused")
        elif env.type == "resume.ordered":
            print(f"[{_now()}] resumed", flush=True)
            self._paused = False
            self._drain_pel_next_step = True  # work parked during the pause
            self.heartbeat(status="idle")

    def drain_corrections(self) -> list[Envelope]:
        corrections, self.pending_corrections = self.pending_corrections, []
        return corrections


def _now() -> str:
    return time.strftime("%H:%M:%S")


def _version() -> str:
    from relay import __version__

    return __version__
