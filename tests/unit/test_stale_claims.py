"""A rescued message is not always work worth doing.

friend-positions, 17:26. `relay down && relay up` brought the workers back,
and the specifier immediately answered four `spec.requested` messages from
09:20 and 10:18 that morning — behaviours built, merged and green for hours.
It cost five model turns to say so, and one of the answers pushed a finished
behaviour back to `planned` on the board.

They arrived through XAUTOCLAIM. A consumer is named `role@host#pid`, so every
restart is a new consumer and the dead one's pending entries are orphans;
`start()` steals anything idle over five minutes and runs it. That is right
for a worker killed mid-turn, and wrong seven hours later: the coordinator
supervises every one of those request types, so it had already re-dispatched
and then escalated them. The rescue was a duplicate, not a retry.

Draining the worker's OWN pending list is a different thing — that is work
that never started, and a long `relay pause` must not lose it.
"""

from __future__ import annotations

from pathlib import Path

from relay.bus import claims, groups
from relay.bus.keys import dlq_key, group_name, ledger_key
from relay.runners.fake import FakeRunner
from relay.workers import base
from relay.workers.chain import ChainWorker

ROLES_DIR = Path(__file__).resolve().parents[2] / "roles"
STREAM = ledger_key("testswarm")
GROUP = group_name("specifier")


def _dlq(client, reason: str) -> list[dict]:
    """DLQ entries for one reason. A FakeRunner reply is not a real on-stream
    result, so these tests also produce ordinary `unparseable` entries — the
    question here is only ever whether something was dropped as superseded."""
    return [e for _id, e in client.xrange(dlq_key("testswarm"))
            if e["reason"] == reason]


def _specifier(client, tmp_path, calls: list[str],
               stale_after_s: float = base.CLAIM_STALE_AFTER_S) -> ChainWorker:
    return ChainWorker(
        "testswarm", "specifier", FakeRunner(lambda p, _s: calls.append(p) or "ok"),
        playbook_path=ROLES_DIR / "specifier.md",
        workspace=tmp_path, state_dir=tmp_path / "s", client=client,
        stale_after_s=stale_after_s,
    )


def _orphaned_spec_request(client, publisher) -> None:
    """A spec.requested delivered to a consumer that then died unacked."""
    publisher.send("coordinator", "specifier", "spec.requested",
                   {"behaviour_id": "I1.S1.B1", "iteration_id": "I1", "kind": "ac",
                    "ac_text": "Only free rooms are listed.", "base_sha": "a" * 40})
    groups.ensure_group(client, STREAM, GROUP)
    groups.read_new(client, STREAM, GROUP, "specifier@host#111", block_ms=1)


def _claim_immediately(monkeypatch) -> None:
    """Age the orphan past XAUTOCLAIM's idle floor without sleeping for it."""
    real = claims.autoclaim_stale
    monkeypatch.setattr(
        base.claims, "autoclaim_stale",
        lambda c, s, g, consumer, **kw: real(c, s, g, consumer, min_idle_ms=0),
    )


def test_a_stale_claimed_request_is_dead_lettered_not_answered(
    client, publisher, tmp_path, monkeypatch
) -> None:
    _orphaned_spec_request(client, publisher)
    _claim_immediately(monkeypatch)

    calls: list[str] = []
    _specifier(client, tmp_path, calls, stale_after_s=-1.0).start()

    assert calls == [], "the model must not be paid to answer a superseded request"
    (entry,) = _dlq(client, "superseded")
    assert "duplicate the turn" in entry["detail"]
    assert client.xpending(STREAM, GROUP)["pending"] == 0, "and it is acked, not left"


def test_a_freshly_claimed_request_is_still_work(
    client, publisher, tmp_path, monkeypatch
) -> None:
    """The point is the age, not the rescue: a worker killed mid-turn must
    still have its message picked up and run."""
    _orphaned_spec_request(client, publisher)
    _claim_immediately(monkeypatch)

    calls: list[str] = []
    _specifier(client, tmp_path, calls).start()

    assert calls, "a recent orphan is exactly what crash recovery is for"
    assert _dlq(client, "superseded") == []


def test_draining_your_own_pending_list_is_never_superseded(
    client, publisher, tmp_path
) -> None:
    """A `relay pause` long enough to age the queue must not bin it: nothing
    in the worker's own PEL was ever started."""
    calls: list[str] = []
    worker = _specifier(client, tmp_path, calls, stale_after_s=-1.0)
    publisher.send("coordinator", "specifier", "spec.requested",
                   {"behaviour_id": "I1.S1.B1", "iteration_id": "I1", "kind": "ac",
                    "ac_text": "Only free rooms are listed.", "base_sha": "a" * 40})
    groups.ensure_group(client, STREAM, worker.group)
    groups.read_new(client, STREAM, worker.group, worker.consumer, block_ms=1)

    worker.start()

    assert calls, "own-PEL work is replayed, however old the pause made it"
    assert _dlq(client, "superseded") == []


def test_rework_is_never_discarded_because_its_retry_is_lossy(
    client, publisher, tmp_path, monkeypatch
) -> None:
    """rework.requested is supervised like the rest, but _redispatch_behaviour
    re-sends a plain build/spec request WITHOUT the findings. Discarding the
    original would throw away the only copy of what the gate actually found."""
    from relay.workers.base import SUPERVISED_REQUESTS

    assert "rework.requested" not in SUPERVISED_REQUESTS

    publisher.send("coordinator", "specifier", "rework.requested",
                   {"behaviour_id": "I1.S1.B1", "attempt": 2,
                    "findings": [{"title": "wrong cap asserted",
                                  "detail": "the cap is 8h, not 8m",
                                  "file": "tests/x.py"}]})
    groups.ensure_group(client, STREAM, GROUP)
    groups.read_new(client, STREAM, GROUP, "specifier@host#111", block_ms=1)
    _claim_immediately(monkeypatch)

    calls: list[str] = []
    _specifier(client, tmp_path, calls, stale_after_s=-1.0).start()

    assert _dlq(client, "superseded") == [], "the findings would go with it"
    assert calls, "it is handled, however old"


def test_the_staleness_bound_clears_every_deadline_the_coordinator_uses(
    tmp_path
) -> None:
    """Discarding a rescued request is only safe once the coordinator has
    certainly re-sent it — so the bound must clear EVERY deadline it
    supervises on, not just dispatch_timeout_s.

    Gates are re-dispatched on their own GateSpec.timeout_s, which the policy
    file openly invites projects to raise. The shipped defaults clear the
    module floor only by coincidence (1800 < 3600).
    """
    import yaml

    from relay.workers.base import CLAIM_STALE_AFTER_S
    from relay.workers.run import _stale_after_s

    (tmp_path / ".relay").mkdir()
    assert _stale_after_s(tmp_path) >= CLAIM_STALE_AFTER_S

    (tmp_path / ".relay" / "gates.yaml").write_text(yaml.safe_dump({
        "dispatch_timeout_s": 900,
        "per_behaviour": [{"gate": "code_review", "role": "reviewer",
                           "timeout_s": 14400}],
    }))
    assert _stale_after_s(tmp_path) > 14400, "a slow gate must outlast the guard"
