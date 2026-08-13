"""Deterministic git operations for the coordinator and toolgate.

The coordinator owns branch creation (models never decide where work lands);
the toolgate owns pinned checkouts (a run physically cannot execute against
the wrong code).
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def head_sha(project: Path) -> str:
    return _git(project, "rev-parse", "HEAD")


def iteration_branch_name(swarm: str, iteration_id: str) -> str:
    return f"relay/{swarm}/{iteration_id.lower()}"


def ensure_iteration_branch(project: Path, swarm: str, iteration_id: str) -> str:
    """Create (or reuse) the iteration branch, check it out, return its head."""
    branch = iteration_branch_name(swarm, iteration_id)
    existing = _git(project, "branch", "--list", branch)
    if not existing:
        _git(project, "branch", branch)
    _git(project, "checkout", "-q", branch)
    return head_sha(project)


def commit_exists(project: Path, sha: str) -> bool:
    try:
        _git(project, "cat-file", "-e", f"{sha}^{{commit}}")
        return True
    except GitError:
        return False


def add_detached_worktree(project: Path, sha: str, dest: Path) -> None:
    _git(project, "worktree", "add", "--detach", str(dest), sha)


def remove_worktree(project: Path, dest: Path) -> None:
    _git(project, "worktree", "remove", "--force", str(dest))
