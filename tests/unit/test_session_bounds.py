"""Sessions are scoped to one work item and hard-capped: the quadratic-cost
guard from the token-burn investigation."""

from pathlib import Path

from relay.runners.base import TurnResult
from relay.workers.chain import MAX_SESSION_TURNS, ChainWorker
from relay.runners.fake import FakeRunner

ROLES_DIR = Path(__file__).resolve().parents[2] / "roles"
SHA = "a" * 40


def _worker(client, publisher, tmp_path, seen_sessions):
    def respond(prompt, session_ref):
        seen_sessions.append(session_ref)
        import re
        event_id = re.search(r"event_id: (\S+)", prompt).group(1)
        publisher.send("builder", "coordinator", "error.raised",
                       {"kind": "other", "detail": "noted"}, in_reply_to=event_id)
        return TurnResult(ok=True, session_ref=f"sess-{len(seen_sessions)}")

    return ChainWorker("testswarm", "builder", FakeRunner(respond),
                       playbook_path=ROLES_DIR / "builder.md",
                       workspace=tmp_path, state_dir=tmp_path / "s", client=client)


def _build_msg(publisher, bid):
    publisher.send("coordinator", "builder", "build.requested",
                   {"behaviour_id": bid, "spec_commit_sha": SHA, "test_paths": ["t.py"]},
                   behaviour_id=bid)


def test_session_reused_within_behaviour_but_rotated_across(client, publisher, tmp_path) -> None:
    seen = []
    worker = _worker(client, publisher, tmp_path, seen)
    worker.start()
    _build_msg(publisher, "I1.S1.B1")
    worker.step(block_ms=1)
    _build_msg(publisher, "I1.S1.B1")   # same behaviour: resume
    worker.step(block_ms=1)
    _build_msg(publisher, "I1.S1.B2")   # new behaviour: fresh session
    worker.step(block_ms=1)
    assert seen[0] is None              # first ever turn: fresh
    assert seen[1] == "sess-1"          # same scope: resumed
    assert seen[2] is None              # scope changed: rotated


def test_session_turn_cap_forces_rotation(client, publisher, tmp_path) -> None:
    seen = []
    worker = _worker(client, publisher, tmp_path, seen)
    worker.start()
    for _ in range(MAX_SESSION_TURNS + 2):
        _build_msg(publisher, "I1.S1.B1")
        worker.step(block_ms=1)
    assert seen[0] is None
    assert all(s is not None for s in seen[1:MAX_SESSION_TURNS])  # resumed up to the cap
    assert seen[MAX_SESSION_TURNS] is None                        # cap: fresh session
