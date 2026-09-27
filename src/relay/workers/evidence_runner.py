"""Execute pytest checks and record their captured results."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import uuid
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Protocol, Any

from relay import receipts
from relay.contract.envelope import Envelope
from relay.gitops import branch as gitops


class ExecutionTarget(Protocol):
    project: Path
    swarm: str
    commands: dict[str, str]
    env: dict[str, str]

    def _complete(
        self,
        env: Envelope,
        exit_code: int,
        duration: float,
        output: str,
        fault: str | None = None,
        receipt: dict[str, Any] | None = None,
    ) -> str: ...


PLUGIN = """import json, os, platform
from pathlib import Path
checks = {}
def pytest_runtest_logreport(report):
    if report.failed or report.skipped or report.when == "call":
        previous = checks.get(report.nodeid)
        if previous not in ("failed", "skipped"):
            checks[report.nodeid] = "skipped" if hasattr(report, "wasxfail") else report.outcome
def pytest_sessionfinish(session, exitstatus):
    Path(os.environ["RELAY_CHECK_REPORT"]).write_text(json.dumps({
        "checks": [{"id": k, "outcome": v} for k, v in sorted(checks.items())],
        "python": platform.python_version(), "pytest": __import__("pytest").__version__
    }))
"""


def execute(gate: ExecutionTarget, env: Envelope) -> str:
    started = time.monotonic()
    try:
        declaration = env.payload["evidence"]
        bindings = declaration["bindings"]
        selectors = receipts.safe_selectors(bindings)
        command = str(env.payload.get("command") or gate.commands.get("evidence", ""))
        if not command:
            raise ValueError("configure commands.evidence as a pytest invocation")
        argv = shlex.split(command)
        if not argv or any("{" in arg for arg in argv):
            raise ValueError(
                "evidence command is an argv prefix; do not use placeholders"
            )
        with tempfile.TemporaryDirectory(prefix="relay-evidence-") as directory:
            root = Path(directory)
            checkout = root / "checkout"
            gitops.add_detached_worktree(
                gate.project, str(env.payload["commit_sha"]), checkout
            )
            try:
                collector = root / "collector"
                collector.mkdir()
                (collector / "relay_receipt_plugin.py").write_text(PLUGIN)
                results = root / "results"
                results.mkdir()
                report = results / "checks.json"
                environment = {
                    "PATH": gate.env.get("PATH", ""),
                    "HOME": str(root),
                    "LANG": "C.UTF-8",
                    "PYTHONHASHSEED": "0",
                    "PYTHONPATH": str(collector),
                    "RELAY_CHECK_REPORT": str(report),
                }
                setup = str(
                    env.payload.get("setup_command") or gate.commands.get("setup", "")
                )
                setup_output = ""
                if setup:
                    boot = run_process(
                        invocation(["sh", "-c", setup], checkout, collector, results),
                        checkout,
                        environment,
                    )
                    setup_output = boot.stdout + boot.stderr
                    if boot.returncode:
                        raise ValueError(
                            "setup failed before verification: " + setup_output[-1500:]
                        )
                argv += ["-p", "relay_receipt_plugin", *selectors]
                sources = {
                    selector.split("::", 1)[0]: hashlib.sha256(
                        (checkout / selector.split("::", 1)[0]).read_bytes()
                    ).hexdigest()
                    for selector in selectors
                }
                for path, baseline in declaration.get("test_baselines", {}).items():
                    if path not in sources:
                        raise ValueError("unselected verification baseline: " + path)
                    original = subprocess.run(
                        ["git", "show", f"{baseline}:{path}"],
                        cwd=gate.project,
                        capture_output=True,
                        check=True,
                    ).stdout
                    if hashlib.sha256(original).hexdigest() != sources[path]:
                        raise ValueError(
                            "verification code changed since independent specification: "
                            + path
                        )
                actual_command = invocation(argv, checkout, collector, results)
                proc = run_process(actual_command, checkout, environment)
                report_data = (
                    json.loads(report.read_text())
                    if report.exists()
                    else {"checks": []}
                )
                observed = report_data["checks"]
                problem = ""
                covered = {}
                try:
                    covered = receipts.coverage(bindings, observed)
                except ValueError as error:
                    problem = str(error)
                result_code = proc.returncode or (1 if problem else 0)
                receipt: dict[str, Any] = {
                    "schema_version": 1,
                    "swarm": gate.swarm,
                    "request_id": env.event_id,
                    "run_id": env.payload["run_id"],
                    "story_id": env.story_id,
                    "version": declaration["version"],
                    "commit_sha": env.payload["commit_sha"],
                    "declaration_digest": receipts.digest(declaration),
                    "command_prefix": shlex.split(command),
                    "command": argv,
                    "execution_command": actual_command,
                    "isolation": "container"
                    if os.environ.get("RELAY_EVIDENCE_IMAGE")
                    else "native",
                    "setup_command": setup,
                    "setup_output": setup_output,
                    "environment": {
                        "platform": platform.platform(),
                        "PYTHONHASHSEED": "0",
                        "python": report_data.get("python", ""),
                        "pytest": report_data.get("pytest", ""),
                    },
                    "test_digests": sources,
                    "checks": observed,
                    "coverage": covered,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                    "exit_code": result_code,
                    "verification_error": problem,
                }
            finally:
                gitops.remove_worktree(gate.project, checkout)
        return gate._complete(
            env,
            exit_code=result_code,
            duration=time.monotonic() - started,
            output=proc.stdout + proc.stderr,
            receipt=receipt,
        )
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        return gate._complete(
            env,
            exit_code=78,
            duration=time.monotonic() - started,
            output=f"evidence execution refused: {error}",
            fault="config_refused",
        )


def container_command(
    image: str, checkout: Path, collector: Path, results: Path, argv: list[str]
) -> list[str]:
    import re

    if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", image):
        raise ValueError(
            "RELAY_EVIDENCE_IMAGE must name an image pinned by sha256 digest"
        )
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        "relay-evidence-" + uuid.uuid4().hex,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        "/tmp:rw,nosuid,size=256m",
        "--mount",
        f"type=bind,src={checkout},dst=/work",
        "--mount",
        f"type=bind,src={collector},dst=/collector,readonly",
        "--mount",
        f"type=bind,src={results},dst=/results",
        "--workdir",
        "/work",
        "--env",
        "HOME=/tmp",
        "--env",
        "PYTHONPATH=/collector",
        "--env",
        "PYTHONHASHSEED=0",
        "--env",
        "RELAY_CHECK_REPORT=/results/checks.json",
        image,
        *argv,
    ]


def invocation(
    argv: list[str], checkout: Path, collector: Path, results: Path
) -> list[str]:
    image = os.environ.get("RELAY_EVIDENCE_IMAGE", "")
    if image:
        return container_command(image, checkout, collector, results, argv)
    if os.environ.get("RELAY_EVIDENCE_ALLOW_NATIVE") != "1":
        raise ValueError(
            "Configure RELAY_EVIDENCE_IMAGE for isolated execution; native execution requires explicit RELAY_EVIDENCE_ALLOW_NATIVE=1"
        )
    return argv


def run_process(
    argv: list[str], checkout: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=checkout,
            env=environment,
            capture_output=True,
            text=True,
            timeout=900,
        )
    finally:
        if argv[:2] == ["docker", "run"] and "--name" in argv:
            name = argv[argv.index("--name") + 1]
            subprocess.run(
                ["docker", "rm", "-f", name], capture_output=True, timeout=15
            )
