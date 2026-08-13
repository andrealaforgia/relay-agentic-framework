"""Foreign/corrupt stream entries (e.g. a v1 swarm that shared the stream
name) must never kill a consumer: workers skip and ack, the coordinator
dead-letters exactly once, the audit reports instead of crashing."""

from __future__ import annotations

from pathlib import Path

from relay.bus import dlq, groups
from relay.bus.keys import group_name, ledger_key
from relay.coordinator.main import Coordinator
from relay.ledger.audit import audit_ledger
from relay.ledger.export import export_jsonl, read_jsonl
from relay.runners.fake import FakeRunner
from relay.workers.chain import ChainWorker

ROLES_DIR = Path(__file__).resolve().parents[2] / "roles"
STREAM = ledger_key("testswarm")

V1_FIELDS = {
    "from": "interpreter", "to": "owner", "type": "progress",
    "body": "No roadmap.md yet - still pre-planning, nothing blocking.",
    "refs": "", "in_reply_to": "", "ts": "2026-08-12T09:30:58+00:00",
}


def _poison(client) -> str:
    return client.xadd(STREAM, V1_FIELDS)


def test_read_new_survives_poison(client, publisher) -> None:
    _poison(client)
    publisher.send("owner", "interpreter", "chat.problem", {"text": "p"})
    groups.ensure_group(client, STREAM, group_name("interpreter"))
    deliveries = groups.read_new(client, STREAM, group_name("interpreter"), "c1", block_ms=1)
    assert len(deliveries) == 2
    assert deliveries[0].envelope is None and deliveries[0].raw["type"] == "progress"
    assert deliveries[1].envelope is not None and deliveries[1].envelope.type == "chat.problem"


def test_chain_worker_skips_and_acks_poison(client, publisher, tmp_path) -> None:
    _poison(client)
    builder = ChainWorker(
        "testswarm", "builder", FakeRunner(lambda _p, _s: "ok"),
        playbook_path=ROLES_DIR / "builder.md",
        workspace=tmp_path, state_dir=tmp_path / "b", client=client,
    )
    builder.start()  # would previously crash here
    assert groups.read_pending(client, STREAM, builder.group, builder.consumer) == []


def test_coordinator_dead_letters_poison_once(client, publisher, tmp_path, project=None) -> None:
    import subprocess
    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=proj, check=True)
    subprocess.run(["git", "-C", str(proj), "commit", "-qm", "init", "--allow-empty"],
                   check=True, env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                                    "PATH": "/usr/bin:/bin"})
    coordinator = Coordinator("testswarm", proj, client=client)
    coordinator.bootstrap()
    _poison(client)
    coordinator.step(block_ms=1)
    assert dlq.dlq_depth(client, "testswarm") == 1
    types = [f["type"] for _s, f in client.xrange(STREAM)]
    assert types.count("system.dlq_routed") == 1
    coordinator.step(block_ms=1)  # consumes its own dlq_routed event; no re-routing
    assert dlq.dlq_depth(client, "testswarm") == 1


def test_audit_reports_unparseable_instead_of_crashing(client, publisher, validator) -> None:
    publisher.send("owner", "interpreter", "chat.problem", {"text": "p"})
    _poison(client)
    report = audit_ledger(client, validator, "testswarm")
    assert report.entries == 2
    rules = [f.rule for f in report.findings]
    assert rules == ["unparseable"]


def test_export_keeps_raw_fidelity_and_import_skips_foreign(client, publisher, tmp_path) -> None:
    publisher.send("owner", "interpreter", "chat.problem", {"text": "p"})
    _poison(client)
    out = tmp_path / "ledger.jsonl"
    assert export_jsonl(client, "testswarm", out) == 2  # both entries exported
    assert len(list(read_jsonl(out))) == 1              # only the valid envelope imports
