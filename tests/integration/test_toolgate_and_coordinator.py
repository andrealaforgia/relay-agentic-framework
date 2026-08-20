"""Toolgate against a real fixture git repo; coordinator loop over fakeredis."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from relay.bus.keys import ledger_key
from relay.contract.envelope import Envelope
from relay.coordinator.main import Coordinator
from relay.coordinator.model import BehaviourState
from relay.gitops import branch as gitops
from relay.workers.toolgate import Toolgate

PYTEST_CMD = f"{sys.executable} -m pytest -q {{test_paths}}"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    _git(proj, "init", "-q", "-b", "main")
    _git(proj, "config", "user.email", "relay@test")
    _git(proj, "config", "user.name", "relay")
    (proj / "README.md").write_text("fixture\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    return proj


def _commit_failing_test(proj: Path) -> str:
    tests = proj / "tests" / "acceptance"
    tests.mkdir(parents=True)
    (tests / "test_b1.py").write_text(
        "from pathlib import Path\n\n"
        "def test_rooms_cli_exists():\n"
        "    assert Path(__file__).parents[2].joinpath('rooms.py').exists()\n"
    )
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "[I1.S1.B1] failing acceptance test")
    return _git(proj, "rev-parse", "HEAD")


def _commit_implementation(proj: Path) -> str:
    (proj / "rooms.py").write_text("print('free rooms')\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "[I1.S1.B1] implement rooms cli")
    return _git(proj, "rev-parse", "HEAD")


def _run_request(publisher, run_id: str, sha: str) -> Envelope:
    publisher.send(
        "coordinator", "toolgate", "run.requested",
        {"run_id": run_id, "kind": "acceptance_test", "commit_sha": sha,
         "test_paths": ["tests/acceptance/test_b1.py"], "behaviour_id": "I1.S1.B1"},
        behaviour_id="I1.S1.B1", commit_sha=sha,
    )
    return None


def _completions(client) -> list[Envelope]:
    return [
        Envelope.from_fields(f)
        for _sid, f in client.xrange(ledger_key("testswarm"))
        if f["type"] == "run.completed"
    ]


def test_toolgate_red_then_green(client, publisher, project: Path) -> None:
    red_sha = _commit_failing_test(project)
    green_sha = _commit_implementation(project)
    gate = Toolgate("testswarm", project, commands={"acceptance_test": PYTEST_CMD}, client=client)

    _run_request(publisher, "run-01J5AB3CDEF4GH5JK6MN7PQ8R1", red_sha)
    _run_request(publisher, "run-01J5AB3CDEF4GH5JK6MN7PQ8R2", green_sha)
    gate.run_forever(block_ms=1, max_cycles=1)

    red, green = _completions(client)
    assert red.payload["exit_code"] != 0        # the failing test really fails at its sha
    assert green.payload["exit_code"] == 0      # and passes once the implementation lands
    assert red.in_reply_to is not None
    assert Path(red.payload["artifact_path"]).exists()
    # the pinned worktree was cleaned up
    assert _git(project, "worktree", "list").count("\n") == 0


def test_toolgate_unknown_sha_fails_loudly(client, publisher, project: Path) -> None:
    gate = Toolgate("testswarm", project, commands={"acceptance_test": PYTEST_CMD}, client=client)
    _run_request(publisher, "run-01J5AB3CDEF4GH5JK6MN7PQ8R3", "f" * 40)
    gate.run_forever(block_ms=1, max_cycles=1)
    (completion,) = _completions(client)
    assert completion.payload["exit_code"] == 127
    assert "not present" in completion.payload["summary"]
    assert completion.payload["fault"] == "missing_commit"


ROADMAP = {
    "iterations": [{
        "id": "I1", "goal": "g", "increment": "a runnable rooms CLI",
        "stories": [{
            "id": "I1.S1", "title": "t", "narrative": "n",
            "acceptance_criteria": [{"id": "I1.S1.B1", "text": "the CLI lists free rooms"}],
        }],
    }],
}


def test_coordinator_loop_dispatches_and_creates_branch(client, publisher, project: Path) -> None:
    coordinator = Coordinator("testswarm", project, client=client)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    coordinator.run_forever(block_ms=1, max_cycles=3)

    assert coordinator.state.behaviours["I1.S1.B1"].state == BehaviourState.SPEC_DISPATCHED
    # the iteration branch exists and is checked out — created by code, not by a model
    assert _git(project, "rev-parse", "--abbrev-ref", "HEAD") == "relay/testswarm/i1"
    types = [f["type"] for _s, f in client.xrange(ledger_key("testswarm"))]
    assert "spec.requested" in types


def test_coordinator_cold_restart_is_exact(client, publisher, project: Path) -> None:
    first = Coordinator("testswarm", project, client=client)
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    first.run_forever(block_ms=1, max_cycles=3)
    dispatched_before = [
        f["type"] for _s, f in client.xrange(ledger_key("testswarm"))
        if f["from"] == "coordinator"
    ]

    second = Coordinator("testswarm", project, client=client)  # cold start, same ledger
    second.run_forever(block_ms=1, max_cycles=3)
    dispatched_after = [
        f["type"] for _s, f in client.xrange(ledger_key("testswarm"))
        if f["from"] == "coordinator"
    ]
    assert dispatched_after == dispatched_before  # replay produced zero new dispatches
    assert second.state.behaviours["I1.S1.B1"].state == BehaviourState.SPEC_DISPATCHED


# ── infrastructure faults: the toolgate says whether the command ran ─────────


def test_toolgate_flags_a_command_that_cannot_run(client, publisher, project: Path) -> None:
    """The whole incident in one test: a non-zero exit that says nothing about
    the code must be labelled as such on the ledger."""
    red_sha = _commit_failing_test(project)
    gate = Toolgate("testswarm", project,
                    commands={"acceptance_test": "definitely-not-a-real-binary -q"},
                    client=client)
    _run_request(publisher, "run-01J5AB3CDEF4GH5JK6MN7PQ8R4", red_sha)
    gate.run_forever(block_ms=1, max_cycles=1)
    (completion,) = _completions(client)
    assert completion.payload["exit_code"] != 0
    assert completion.payload["fault"] == "not_executable"


def test_toolgate_flags_a_launcher_that_could_not_spawn(client, publisher, project: Path) -> None:
    """`uv run pytest` exits 2, not 127 — the exit code alone never catches it."""
    red_sha = _commit_failing_test(project)
    spawn_failure = (
        "printf 'error: Failed to spawn: `pytest`\\n"
        "  Caused by: No such file or directory (os error 2)\\n' >&2; exit 2"
    )
    gate = Toolgate("testswarm", project, commands={"acceptance_test": spawn_failure},
                    client=client)
    _run_request(publisher, "run-01J5AB3CDEF4GH5JK6MN7PQ8R5", red_sha)
    gate.run_forever(block_ms=1, max_cycles=1)
    (completion,) = _completions(client)
    assert completion.payload["fault"] == "not_executable"


def test_toolgate_never_flags_a_genuinely_failing_test(client, publisher, project: Path) -> None:
    red_sha = _commit_failing_test(project)
    gate = Toolgate("testswarm", project, commands={"acceptance_test": PYTEST_CMD}, client=client)
    _run_request(publisher, "run-01J5AB3CDEF4GH5JK6MN7PQ8R6", red_sha)
    gate.run_forever(block_ms=1, max_cycles=1)
    (completion,) = _completions(client)
    assert completion.payload["exit_code"] != 0
    assert "fault" not in completion.payload      # a real red is real evidence


def test_toolgate_has_no_opinion_about_the_stack(client, publisher, project: Path) -> None:
    """No configured command means no run — never somebody else's test runner."""
    red_sha = _commit_failing_test(project)
    gate = Toolgate("testswarm", project, client=client)      # nothing configured
    _run_request(publisher, "run-01J5AB3CDEF4GH5JK6MN7PQ8R7", red_sha)
    gate.run_forever(block_ms=1, max_cycles=1)
    (completion,) = _completions(client)
    assert completion.payload["fault"] == "no_command"
    assert "no command configured" in completion.payload["summary"]


def test_toolgate_prefers_the_command_on_the_work_item(client, publisher, project: Path) -> None:
    """The plan's command travels with the run; the worker's own config is only
    a fallback, so a stale process cannot outvote the approved plan."""
    red_sha = _commit_failing_test(project)
    gate = Toolgate("testswarm", project,
                    commands={"acceptance_test": "exit 3"}, client=client)
    publisher.send(
        "coordinator", "toolgate", "run.requested",
        {"run_id": "run-01J5AB3CDEF4GH5JK6MN7PQ8R8", "kind": "acceptance_test",
         "commit_sha": red_sha, "command": "exit 7", "behaviour_id": "I1.S1.B1"},
        behaviour_id="I1.S1.B1", commit_sha=red_sha,
    )
    gate.run_forever(block_ms=1, max_cycles=1)
    (completion,) = _completions(client)
    assert completion.payload["exit_code"] == 7


# ── the framework has no default stack ──────────────────────────────────────


def test_relay_init_names_no_test_runner(project: Path) -> None:
    """A project is initialized before anyone knows what it is written in, so
    init must not guess. It once wrote `uv run pytest` into every project."""
    import tomllib

    from relay.cli.main import _initialize

    _initialize(project, "testswarm", "")
    text = (project / ".relay" / "relay.toml").read_text()
    config = tomllib.loads(text)                    # still valid TOML
    assert config.get("commands", {}) == {}         # nothing chosen for you
    assert "pytest" not in text and "cargo" not in text.split("# e.g.")[0]


def test_doctor_flags_a_toolchain_the_toolgate_cannot_reach(
    project: Path, monkeypatch, capsys
) -> None:
    """The check that turns 'cargo is not on the worker's PATH' into one line
    at the start, instead of a run log nobody reads."""
    from relay.cli.main import _check_toolchain, _initialize

    _initialize(project, "testswarm", "definitely-not-a-real-binary test")
    monkeypatch.chdir(project)
    assert _check_toolchain() == 1
    assert "is not on the toolgate's PATH" in capsys.readouterr().out
