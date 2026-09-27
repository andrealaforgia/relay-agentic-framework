"""A real runner process reviews the requested revision and delivers its verdict."""

import json
import subprocess
import sys
from pathlib import Path

from relay.contract.envelope import Envelope
from relay.ledger.reader import read_all
from relay.runners.codex import CodexRunner
from relay.workers.chain import ChainWorker


def test_codex_review_is_pinned_and_delivered_to_the_interpreter(
    client, publisher, tmp_path
):
    project = tmp_path / "project"
    project.mkdir()

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=project, text=True).strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.org")
    for content in ["original", "broken", "later work"]:
        (project / "implementation.txt").write_text(content)
        git("add", ".")
        git("commit", "-qm", content)
        if content == "original":
            base = git("rev-parse", "HEAD")
        if content == "broken":
            candidate = git("rev-parse", "HEAD")
    gate_id = "gate-01J5AB3CDEF4GH5JK6MN7PQ8R1"
    report = {
        "gate_id": gate_id,
        "verdict": "fail",
        "summary": "The requested revision is broken.",
        "limitations": ["Only the fixture behaviour was inspected."],
        "findings": [
            {
                "title": "Broken implementation",
                "detail": "The implementation returns broken.",
                "file": "implementation.txt",
                "line": 1,
                "severity": "major",
            }
        ],
    }
    binary = tmp_path / "codex"
    binary.write_text(f"""#!{sys.executable}
import json, subprocess
from pathlib import Path
assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() == {candidate!r}
assert Path('implementation.txt').read_text() == 'broken'
print(json.dumps({{'type': 'item.completed', 'item': {{'type': 'agent_message', 'text': {json.dumps(report)!r}}}}}))
print(json.dumps({{'type': 'turn.completed'}}))
""")
    binary.chmod(0o755)
    worker = ChainWorker(
        "testswarm",
        "reviewer",
        CodexRunner(binary=str(binary), model="review-model"),
        Path("roles/reviewer.md"),
        project,
        tmp_path / "state",
        client,
    )
    publisher.send(
        "coordinator",
        "reviewer",
        "gate.requested",
        {
            "gate_id": gate_id,
            "gate": "code_review",
            "subject_kind": "behaviour",
            "subject_id": "I1.S1.B1",
            "commit_sha": candidate,
            "base_sha": base,
        },
        behaviour_id="I1.S1.B1",
        story_id="I1.S1",
        iteration_id="I1",
    )
    request = list(read_all(client, "testswarm"))[-1][1]
    assert isinstance(request, Envelope)
    worker.handle(request)
    events = [env for _, env in read_all(client, "testswarm")]
    verdict = next(env for env in events if env.type == "gate.judged")
    forwarded = next(env for env in events if env.type == "update.shared")
    assert verdict.payload == report
    assert forwarded.to_role == "interpreter"
    assert forwarded.in_reply_to == verdict.event_id
    assert "review-model" in forwarded.payload["text"]
    assert candidate in forwarded.payload["text"]
    assert (project / "implementation.txt").read_text() == "later work"
