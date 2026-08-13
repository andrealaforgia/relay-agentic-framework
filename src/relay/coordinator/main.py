"""The coordinator process: replay -> project -> react -> consume -> react.

State is never persisted (D3): a cold start replays the full ledger, which
also reconstructs every dispatch this process ever made, so react() resumes
mid-behaviour without double-dispatching.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import redis

from relay.bus import groups
from relay.bus.client import get_client
from relay.bus.keys import group_name, ledger_key, presence_key
from relay.bus.publisher import Publisher
from relay.contract import ContractValidator, load_contract
from relay.coordinator.dispatcher import Dispatcher, GitHooks
from relay.coordinator.model import SwarmState
from relay.coordinator.policy import Policy
from relay.coordinator.projection import apply
from relay.gitops import branch as gitops
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
            ),
        )
        self.state = SwarmState()
        self.stream = ledger_key(swarm)
        self.group = group_name("coordinator")
        self.consumer = f"coordinator@{socket.gethostname()}#{os.getpid()}"
        self._stopping = False

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
            apply(self.state, delivery.envelope)
            groups.ack(self.client, self.stream, self.group, delivery.stream_id)
        if deliveries:
            self.dispatcher.react(self.state)
        self.client.set(
            presence_key(self.swarm, "coordinator", socket.gethostname()),
            str(os.getpid()),
            ex=PRESENCE_TTL_S,
        )
        return len(deliveries)

    def run_forever(self, block_ms: int = 5000, max_cycles: int | None = None) -> None:
        self.bootstrap()
        cycles = 0
        while not self._stopping:
            self.step(block_ms=block_ms)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
