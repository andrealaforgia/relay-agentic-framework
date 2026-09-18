"""A project can work on trunk: no iteration branches, no pull requests.

Relay put every iteration on its own branch, relay/<swarm>/<iteration>, and
offered a pull request as the way to land it. A trunk-based project wants the
opposite: every commit straight to its main line, verified before it is
pushed. With `trunk: <branch>` in the project's gate policy, the coordinator
works on that branch, never creates another, never opens a pull request, and
says so when an iteration finishes. Without it nothing changes, so other
projects keep their branches.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from test_coordinator import ROADMAP, MiniSwarm, _drive_behaviour_to_done

from relay.coordinator.dispatcher import GitHooks
from relay.coordinator.main import Coordinator
from relay.coordinator.policy import Policy
from relay.gitops.branch import GitError, ensure_work_branch


def _repo(tmp_path: Path) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    (tmp_path / "README.md").write_text("x\n")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "init")
    return tmp_path


def _branches(repo: Path) -> list[str]:
    out = subprocess.run(["git", "branch", "--format=%(refname:short)"], cwd=repo,
                         check=True, capture_output=True, text=True).stdout
    return sorted(out.split())


def _current(repo: Path) -> str:
    return subprocess.run(["git", "branch", "--show-current"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


# ── the policy setting ───────────────────────────────────────────────────────

def test_trunk_is_off_unless_a_project_asks_for_it(tmp_path: Path) -> None:
    assert Policy().trunk == ""
    path = tmp_path / "gates.yaml"
    path.write_text("wip_limit: 1\n")
    assert Policy.load(path).trunk == ""


def test_a_project_names_its_trunk_in_its_policy(tmp_path: Path) -> None:
    path = tmp_path / "gates.yaml"
    path.write_text("trunk: main\n")
    assert Policy.load(path).trunk == "main"


# ── where the work lands ─────────────────────────────────────────────────────

def test_on_trunk_an_iteration_works_on_trunk_and_creates_no_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ensure_work_branch(repo, "prova", "I4", trunk="main")
    assert _branches(repo) == ["main"]
    assert _current(repo) == "main"


def test_without_trunk_an_iteration_still_gets_its_own_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ensure_work_branch(repo, "prova", "I4", trunk="")
    assert "relay/prova/i4" in _branches(repo)
    assert _current(repo) == "relay/prova/i4"


def test_a_trunk_that_does_not_exist_is_refused_not_created(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(GitError):
        ensure_work_branch(repo, "prova", "I4", trunk="trunk-typo")
    assert _branches(repo) == ["main"]


def test_the_coordinator_wires_its_project_policy_to_trunk(tmp_path: Path, client) -> None:
    (tmp_path / "repo").mkdir()
    repo = _repo(tmp_path / "repo")
    policy = tmp_path / "gates.yaml"
    policy.write_text("trunk: main\n")
    coordinator = Coordinator("testswarm", repo, policy_path=policy, client=client)
    coordinator.dispatcher._git.ensure_branch("I4")
    assert _branches(repo) == ["main"]


# ── the end of an iteration ──────────────────────────────────────────────────

def _finished(client, publisher, policy: Policy) -> tuple[MiniSwarm, list[str]]:
    prs: list[str] = []
    swarm = MiniSwarm(client, publisher, policy=policy, git=GitHooks(
        ensure_branch=lambda _i: "a" * 40, head_sha=lambda: "a" * 40,
        has_history=lambda: False, create_pr=lambda it: prs.append(it) or "https://x/pull/1"))
    publisher.send("interpreter", "coordinator", "roadmap.committed",
                   {"roadmap": ROADMAP, "intake": {"mode": "greenfield"}})
    publisher.send("interpreter", "coordinator", "iteration.started", {"iteration_id": "I1"})
    swarm.pump()
    for bid in ("I1.S1.B1", "I1.S1.INT", "I1.INT"):
        _drive_behaviour_to_done(swarm, bid)
    return swarm, prs


def test_on_trunk_a_finished_iteration_says_it_has_already_landed(client, publisher) -> None:
    swarm, _ = _finished(client, publisher, Policy(trunk="main"))
    (finished,) = swarm.sent("iteration.finished")
    assert finished.payload["landed_on"] == "main"


def test_on_trunk_no_pull_request_is_ever_opened(client, publisher) -> None:
    swarm, prs = _finished(client, publisher, Policy(trunk="main"))
    publisher.send("interpreter", "coordinator", "pr.approved",
                   {"iteration_id": "I1", "gate_id": "gate-01M08YB2FF5X6KHTRJXR948MD9"})
    swarm.pump()
    assert prs == []
    assert swarm.sent("pr.opened") == []


def test_without_trunk_the_pull_request_path_is_unchanged(client, publisher) -> None:
    swarm, prs = _finished(client, publisher, Policy())
    (finished,) = swarm.sent("iteration.finished")
    assert "landed_on" not in finished.payload
    publisher.send("interpreter", "coordinator", "pr.approved",
                   {"iteration_id": "I1", "gate_id": "gate-01M08YB2FF5X6KHTRJXR948MD9"})
    swarm.pump()
    assert prs == ["I1"]
