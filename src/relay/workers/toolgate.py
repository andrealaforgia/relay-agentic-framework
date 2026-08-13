"""The toolgate: deterministic run execution.

Consumes run.requested, executes the command in a detached worktree pinned to
the requested SHA (running against the wrong code is physically impossible),
publishes run.completed with machine-verified evidence. No model is involved.
"""

from __future__ import annotations

import hashlib
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

import redis

from relay.contract.envelope import Envelope
from relay.gitops import branch as gitops
from relay.workers.base import Worker

DEFAULT_COMMANDS = {
    "acceptance_test": "python3 -m pytest -q {test_paths}",
    "suite": "python3 -m pytest -q",
}
RUN_TIMEOUT_S = 900


class Toolgate(Worker):
    def __init__(
        self,
        swarm: str,
        project: Path,
        commands: dict[str, str] | None = None,
        client: redis.Redis | None = None,
    ) -> None:
        super().__init__(swarm, "toolgate", client=client)
        self.project = project
        self.commands = {**DEFAULT_COMMANDS, **(commands or {})}
        self.artifacts_dir = project / ".relay" / "runs"

    def handle(self, env: Envelope) -> str | None:
        if env.type != "run.requested":
            return None
        payload = env.payload
        run_id = str(payload["run_id"])
        kind = str(payload["kind"])
        sha = str(payload["commit_sha"])

        started = time.monotonic()
        if not gitops.commit_exists(self.project, sha):
            return self._complete(env, exit_code=127, duration=0.0,
                                  output=f"commit {sha} not present in {self.project}")

        command = str(payload.get("command") or "").strip() or self.commands.get(kind, "")
        if not command:
            return self._complete(env, exit_code=127, duration=0.0,
                                  output=f"no command configured for run kind '{kind}'")
        test_paths = " ".join(shlex.quote(p) for p in payload.get("test_paths") or [])
        command = command.replace("{test_paths}", test_paths)

        with tempfile.TemporaryDirectory(prefix=f"relay-{run_id}-") as tmp:
            worktree = Path(tmp) / "checkout"
            gitops.add_detached_worktree(self.project, sha, worktree)
            try:
                proc = subprocess.run(
                    command, shell=True, cwd=worktree,
                    capture_output=True, text=True, timeout=RUN_TIMEOUT_S,
                )
                exit_code, output = proc.returncode, proc.stdout + proc.stderr
            except subprocess.TimeoutExpired:
                exit_code, output = 124, f"timed out after {RUN_TIMEOUT_S}s"
            finally:
                gitops.remove_worktree(self.project, worktree)

        return self._complete(env, exit_code=exit_code,
                              duration=time.monotonic() - started, output=output)

    def _complete(self, env: Envelope, exit_code: int, duration: float, output: str) -> str:
        run_id = str(env.payload["run_id"])
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        artifact = self.artifacts_dir / f"{run_id}.log"
        artifact.write_text(output)
        result = self.publisher.send(
            "toolgate", "coordinator", "run.completed",
            {
                "run_id": run_id,
                "kind": str(env.payload["kind"]),
                "commit_sha": str(env.payload["commit_sha"]),
                "exit_code": exit_code,
                "duration_s": round(duration, 3),
                "output_digest": hashlib.sha256(output.encode()).hexdigest(),
                "artifact_path": str(artifact),
                "summary": output[-1500:].strip() or "(no output)",
            },
            in_reply_to=env.event_id,
            behaviour_id=env.behaviour_id,
            iteration_id=env.iteration_id,
            story_id=env.story_id,
            commit_sha=str(env.payload["commit_sha"]),
        )
        return result.event_id
