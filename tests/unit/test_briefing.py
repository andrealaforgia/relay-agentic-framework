"""Deterministic code hands over what it already knows.

The measured failure this fixes: one specifier turn ran 37 agentic loops and
re-read 3.5M tokens to produce a single acceptance test, because it had to
discover the project from nothing. Every loop re-sends the accumulated
context, so rediscovery is charged quadratically. The coordinator had the
SHAs, the payload had the paths, and the brief was sitting on disk.
"""

import subprocess
from pathlib import Path

from relay.workers import briefing


def _repo(tmp_path: Path) -> Path:
    work = tmp_path / "project"
    (work / "src").mkdir(parents=True)
    run = lambda *a: subprocess.run(a, cwd=work, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "T")
    (work / "src" / "board.py").write_text("def place():\n    pass\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "base")
    return work


def _sha(work: Path, rev: str = "HEAD") -> str:
    return subprocess.run(["git", "rev-parse", rev], cwd=work,
                          capture_output=True, text=True).stdout.strip()


def test_a_gate_is_handed_the_diff_it_was_about_to_compute(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    base = _sha(work)
    (work / "src" / "board.py").write_text("def place(mark):\n    return mark\n")
    subprocess.run(["git", "commit", "-aqm", "place a mark"], cwd=work, check=True)
    head = _sha(work)

    text = briefing.build(work, "gate.requested", {"base_sha": base, "commit_sha": head})
    assert "The change under review" in text
    assert "def place(mark)" in text          # the actual change
    assert "src/board.py" in text             # the stat line
    assert base[:8] in text and head[:8] in text


def test_a_builder_is_handed_the_test_it_must_satisfy(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    (work / "tests").mkdir()
    (work / "tests" / "test_place.py").write_text("def test_place():\n    assert place('X')\n")

    text = briefing.build(work, "build.requested", {"test_paths": ["tests/test_place.py"]})
    assert "assert place('X')" in text
    assert "Never edit these files" in text


def test_the_reconnaissance_brief_reaches_the_workers(tmp_path: Path) -> None:
    """Iteration 0 pays a model to write this. Until now nobody read it."""
    work = _repo(tmp_path)
    (work / "docs").mkdir()
    (work / "docs" / "codebase-brief.md").write_text("# Brief\nThe board lives in src/board.py.")

    for type_ in ("spec.requested", "build.requested", "gate.requested"):
        assert "The board lives in src/board.py" in briefing.build(work, type_, {})


def test_oversized_context_is_clipped_and_says_so(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    (work / "docs").mkdir()
    (work / "docs" / "codebase-brief.md").write_text("x" * (briefing.BRIEF_BUDGET * 3))

    text = briefing.build(work, "spec.requested", {})
    assert "truncated" in text
    assert len(text) < briefing.BRIEF_BUDGET * 2      # bounded, not a repo dump


def test_a_missing_repo_or_brief_is_silence_not_a_crash(tmp_path: Path) -> None:
    empty = tmp_path / "nothing"
    empty.mkdir()
    assert briefing.build(empty, "gate.requested",
                          {"base_sha": "a" * 40, "commit_sha": "b" * 40}) == ""
    assert briefing.build(empty, "build.requested", {"test_paths": ["nope.py"]}) == ""
    assert briefing.build(empty, "spec.requested", {}) == ""


def test_the_playbook_stays_first_in_the_prompt(client, publisher, tmp_path) -> None:
    """Cache prefixes are byte-prefixes: anything that varies per turn must
    come after the part that never varies."""
    from relay.runners.base import TurnResult
    from relay.runners.fake import FakeRunner
    from relay.workers.chain import ChainWorker

    roles = Path(__file__).resolve().parents[2] / "roles"
    seen: list[str] = []

    def respond(prompt, session_ref):
        seen.append(prompt)
        return TurnResult(ok=True, session_ref="s1")

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "codebase-brief.md").write_text("# Brief\nBoard lives in src/board.py.")
    worker = ChainWorker("testswarm", "specifier", FakeRunner(respond),
                         playbook_path=roles / "specifier.md",
                         workspace=tmp_path, state_dir=tmp_path / "s", client=client)
    worker.start()
    publisher.send("coordinator", "specifier", "spec.requested",
                   {"behaviour_id": "I1.S1.B1", "iteration_id": "I1",
                    "ac_text": "Given an empty board…", "kind": "ac", "base_sha": "b" * 40},
                   behaviour_id="I1.S1.B1")
    worker.step(block_ms=1)

    assert seen, "the runner was never invoked"
    prompt = seen[0]
    assert prompt.startswith("# Specifier")          # playbook first, always
    # then the trigger, then the briefing: static before volatile
    assert prompt.index("# Specifier") < prompt.index("Relay protocol") \
        < prompt.index("Board lives in src/board.py")
