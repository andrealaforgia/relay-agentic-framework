"""The coordinator process: replay -> project -> react -> consume -> react.

State is never persisted (D3): a cold start replays the full ledger, which
also reconstructs every dispatch this process ever made, so react() resumes
mid-behaviour without double-dispatching.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import redis

from relay.bus import dlq, groups
from relay.bus.client import get_client
from relay.bus.keys import group_name, ledger_key, presence_key
from relay.bus.publisher import Publisher
from relay.contract import ContractValidator, load_contract
from relay.coordinator.dispatcher import Dispatcher, GitHooks
from relay.coordinator.model import SwarmState
from relay.coordinator.policy import Policy
from relay.coordinator.projection import apply
from relay.gitops import branch as gitops
from relay.gitops import pr as pr_mod
from relay.ledger.reader import read_all

PRESENCE_TTL_S = 45


class Coordinator:
    def __init__(
        self,
        swarm: str,
        project: Path,
        policy_path: Path | None = None,
        client: redis.Redis | None = None,
    ) -> None:
        self.swarm = swarm
        self.project = project
        self.client = client if client is not None else get_client()
        self.validator = ContractValidator(load_contract())
        self.publisher = Publisher(self.client, self.validator, swarm)
        policy = Policy.load(policy_path) if policy_path else Policy()
        self.dispatcher = Dispatcher(
            self.publisher,
            policy,
            GitHooks(
                ensure_branch=lambda it: gitops.ensure_iteration_branch(project, swarm, it),
                head_sha=lambda: gitops.head_sha(project),
                has_history=lambda: gitops.has_history(project),
                create_pr=lambda it: pr_mod.create_pr(
                    project, swarm, it,
                    title=f"Relay {swarm} — iteration {it}",
                    body=f"Iteration {it} delivered by the relay swarm '{swarm}'. "
                         f"See the ledger for the full audit trail.\n\n"
                         f"🤖 Generated with the Relay Agentic Framework",
                ),
            ),
        )
        self.state = SwarmState()
        self.stream = ledger_key(swarm)
        self.group = group_name("coordinator")
        self.consumer = f"coordinator@{socket.gethostname()}#{os.getpid()}"
        self._stopping = False
        self._started = time.time()

    def bootstrap(self) -> None:
        """Full replay for state; the consumer group only signals new arrivals."""
        groups.ensure_group(self.client, self.stream, self.group)
        for _sid, env in read_all(self.client, self.swarm):
            apply(self.state, env)
        # everything replayed is by definition processed: clear our PEL backlog
        for delivery in groups.read_pending(self.client, self.stream, self.group, self.consumer):
            groups.ack(self.client, self.stream, self.group, delivery.stream_id)
        self.dispatcher.react(self.state)

    def stop(self) -> None:
        self._stopping = True

    def step(self, block_ms: int = 5000) -> int:
        """One read-apply-react cycle. Returns how many events were consumed."""
        deliveries = groups.read_new(
            self.client, self.stream, self.group, self.consumer, block_ms=block_ms
        )
        for delivery in deliveries:
            if delivery.envelope is None:
                # the coordinator owns quarantine: dead-letter foreign/corrupt
                # entries exactly once, loudly (other consumers just skip them)
                dlq.route_to_dlq(
                    self.client, self.publisher, self.swarm, "coordinator",
                    "unparseable", delivery.raw,
                    f"stream entry {delivery.stream_id} is not a v2 envelope",
                )
            else:
                apply(self.state, delivery.envelope)
            groups.ack(self.client, self.stream, self.group, delivery.stream_id)
        if deliveries:
            self.dispatcher.react(self.state)
        self.client.set(
            presence_key(self.swarm, "coordinator", socket.gethostname()),
            json.dumps({"pid": os.getpid(), "status": "coordinating", "since": self._started}),
            ex=PRESENCE_TTL_S,
        )
        return len(deliveries)

    def run_forever(self, block_ms: int = 5000, max_cycles: int | None = None) -> None:
        self.bootstrap()
        cycles = 0
        backoff = 1.0
        while not self._stopping:
            try:
                self.step(block_ms=block_ms)
                backoff = 1.0
            except redis.RedisError as e:
                print(f"!! redis error ({e}) — retrying in {backoff:.0f}s", flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
