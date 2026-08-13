"""PR creation via gh, run by deterministic code on the owner's approval only."""

from __future__ import annotations

import subprocess
from pathlib import Path

from relay.gitops.branch import GitError, iteration_branch_name


def default_branch(project: Path) -> str:
    result = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
        cwd=project, capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().split("/", 1)[-1]
    return "main"


def create_pr(project: Path, swarm: str, iteration_id: str, title: str, body: str) -> str:
    branch = iteration_branch_name(swarm, iteration_id)
    push = subprocess.run(
        ["git", "push", "-u", "origin", branch], cwd=project, capture_output=True, text=True
    )
    if push.returncode != 0:
        raise GitError(f"push {branch}: {push.stderr.strip()}")
    result = subprocess.run(
        ["gh", "pr", "create", "--base", default_branch(project), "--head", branch,
         "--title", title, "--body", body],
        cwd=project, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise GitError(f"gh pr create: {result.stderr.strip()}")
    return result.stdout.strip().splitlines()[-1]
