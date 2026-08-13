"""The worker lifecycle every role shares.

Startup order is always: drain own PEL -> XAUTOCLAIM stale entries from dead
consumers -> block on '>'. A message is acked only when handling produced a
verifiable result (subclasses define what that means); messages redelivered
past the delivery cap go to the DLQ — loudly, never silently.
"""

from __future__ import annotations

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
            self.role, "system", "system.worker_started",
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
            self.role, "system", "system.worker_stopped",
            {"role": self.role, "host": socket.gethostname(), "pid": os.getpid()},
        )

    def stop(self) -> None:
        self._stopping = True

    def heartbeat(self) -> None:
        self.client.set(
            presence_key(self.swarm, self.role, socket.gethostname()),
            str(os.getpid()),
            ex=PRESENCE_TTL_S,
        )

    def run_forever(self, block_ms: int = 5000, max_cycles: int | None = None) -> None:
        groups.ensure_group(self.client, self.stream, self.group)
        self.announce_started()
        self.heartbeat()

        for delivery in groups.read_pending(self.client, self.stream, self.group, self.consumer):
            self._process(delivery)
        for delivery in claims.autoclaim_stale(self.client, self.stream, self.group, self.consumer):
            self._process(delivery)

        cycles = 0
        while not self._stopping:
            for delivery in groups.read_new(
                self.client, self.stream, self.group, self.consumer, block_ms=block_ms
            ):
                self._process(delivery)
            self.heartbeat()
            self.on_tick()
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
        self.announce_stopped()

    # ── per-delivery processing ──────────────────────────────────────────────

    def _process(self, delivery: groups.Delivery) -> None:
        env = delivery.envelope
        if not self.wants(env):
            groups.ack(self.client, self.stream, self.group, delivery.stream_id)
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

        result_id = self.handle(env)
        if result_id is not None:
            dedup.mark_done(self.client, self.swarm, self.role, env.event_id, result_id)
        groups.ack(self.client, self.stream, self.group, delivery.stream_id)


def _version() -> str:
    from relay import __version__

    return __version__
