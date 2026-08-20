"""The toolgate: deterministic run execution.

Consumes run.requested, executes the command in a detached worktree pinned to
the requested SHA (running against the wrong code is physically impossible),
publishes run.completed with machine-verified evidence. No model is involved.

It holds NO opinion about the project's stack. The command travels with the
work item, put there by the coordinator from the iteration's approved change
plan; `commands` here is only a local fallback for projects that configure the
toolgate directly. Nothing is defaulted: a run kind with no command is a fault,
not an excuse to run somebody else's test runner.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

import redis

from relay.contract.envelope import Envelope
from relay.gitops import branch as gitops
from relay.workers import faults
from relay.workers.base import Worker

RUN_TIMEOUT_S = 900


class Toolgate(Worker):
    def __init__(
        self,
        swarm: str,
        project: Path,
        commands: dict[str, str] | None = None,
        client: redis.Redis | None = None,
        *,
        path_extra: str | None = None,
        inherit_login_path: bool = True,
    ) -> None:
        super().__init__(swarm, "toolgate", client=client)
        self.project = project
        self.commands = dict(commands or {})
        self.artifacts_dir = project / ".relay" / "runs"
        self.env = self._environment(path_extra, inherit_login_path)

    @staticmethod
    def _environment(path_extra: str | None, inherit_login_path: bool) -> dict[str, str]:
        env = dict(os.environ)
        extra = path_extra or (faults.login_shell_path() if inherit_login_path else None)
        if extra:
            env["PATH"] = faults.merge_path(env.get("PATH", ""), extra)
        return env

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
                                  output=f"commit {sha} not present in {self.project}",
                                  fault=faults.MISSING_COMMIT)

        # the work item wins: a stale worker must never outvote the approved plan
        command = str(payload.get("command") or "").strip() or self.commands.get(kind, "")
        if not command:
            return self._complete(env, exit_code=127, duration=0.0,
                                  output=f"no command configured for run kind '{kind}'",
                                  fault=faults.NO_COMMAND)
        test_paths = " ".join(shlex.quote(p) for p in payload.get("test_paths") or [])
        command = command.replace("{test_paths}", test_paths)

        with tempfile.TemporaryDirectory(prefix=f"relay-{run_id}-") as tmp:
            worktree = Path(tmp) / "checkout"
            gitops.add_detached_worktree(self.project, sha, worktree)
            try:
                proc = subprocess.run(
                    command, shell=True, cwd=worktree, env=self.env,
                    capture_output=True, text=True, timeout=RUN_TIMEOUT_S,
                )
                exit_code, output = proc.returncode, proc.stdout + proc.stderr
                fault = faults.classify(exit_code, output)
            except subprocess.TimeoutExpired:
                exit_code, output = 124, f"timed out after {RUN_TIMEOUT_S}s"
                fault = faults.TIMEOUT
            finally:
                gitops.remove_worktree(self.project, worktree)

        return self._complete(env, exit_code=exit_code, fault=fault,
                              duration=time.monotonic() - started, output=output)

    def _complete(
        self,
        env: Envelope,
        exit_code: int,
        duration: float,
        output: str,
        fault: str | None = None,
    ) -> str:
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
                **({"fault": fault} if fault else {}),
            },
            in_reply_to=env.event_id,
            behaviour_id=env.behaviour_id,
            iteration_id=env.iteration_id,
            story_id=env.story_id,
            commit_sha=str(env.payload["commit_sha"]),
        )
        return result.event_id
