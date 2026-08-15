"""Detached worker process management: pidfiles under ~/.relay/<swarm>/run/,
logs under ~/.relay/<swarm>/logs/. Kill by pidfile, never by pattern-matching
command lines (a v1 failure mode)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# the interpreter is NOT here: it lives inside `relay chat` as a live session.
# the sentinel is opt-in for now (Andrea, 2026-08-13): start it explicitly with
#   relay up . --roles coordinator,toolgate,analyst,specifier,builder,sentinel
PHASE1_ROLES = ("coordinator", "toolgate", "analyst", "specifier", "builder")


def state_root() -> Path:
    return Path(os.environ.get("RELAY_STATE_ROOT", str(Path.home() / ".relay")))


def run_dir(swarm: str) -> Path:
    return state_root() / swarm / "run"


def log_dir(swarm: str) -> Path:
    return state_root() / swarm / "logs"


def pidfile(swarm: str, role: str) -> Path:
    return run_dir(swarm) / f"{role}.pid"


def logfile(swarm: str, role: str) -> Path:
    return log_dir(swarm) / f"{role}.log"


def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _arg_value(command: str, flag: str) -> str | None:
    tokens = command.split()
    for i, token in enumerate(tokens[:-1]):
        if token == flag:
            return tokens[i + 1]
    return None


def _worker_processes() -> list[tuple[int, str, str]]:
    """(pid, swarm, role) of every live relay worker on this machine."""
    result = subprocess.run(["ps", "-eo", "pid=,command="], capture_output=True, text=True)
    workers: list[tuple[int, str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or "relay.workers.run" not in parts[1]:
            continue
        swarm = _arg_value(parts[1], "--swarm")
        role = _arg_value(parts[1], "--role")
        if swarm and role:
            workers.append((int(parts[0]), swarm, role))
    return workers


def find_workers(swarm: str, role: str) -> list[int]:
    """Every live relay worker process for (swarm, role) — exact argv match
    on our own module invocation, tracked by a pidfile or not."""
    return [pid for pid, s, r in _worker_processes() if s == swarm and r == role]


def reap_orphans(swarm: str, role: str, keep: int | None = None) -> list[int]:
    """Kill worker processes for (swarm, role) that we are not tracking.

    Zombie twins happen when state under ~/.relay is deleted while workers
    are alive: the pidfiles vanish, `relay up` starts fresh workers, and the
    orphans keep consuming the same group with stale code — silently eating
    events. One (swarm, role) must be exactly one process.
    """
    reaped = []
    for pid in find_workers(swarm, role):
        if pid == keep or pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            reaped.append(pid)
        except (ProcessLookupError, PermissionError):
            pass
    return reaped


def start_worker(swarm: str, role: str, project: Path) -> int:
    run_dir(swarm).mkdir(parents=True, exist_ok=True)
    log_dir(swarm).mkdir(parents=True, exist_ok=True)
    pf = pidfile(swarm, role)
    if pf.exists() and is_running(int(pf.read_text())):
        reap_orphans(swarm, role, keep=int(pf.read_text()))
        return int(pf.read_text())
    reap_orphans(swarm, role)  # nothing tracked: any survivor is a zombie
    log = open(logfile(swarm, role), "a")  # noqa: SIM115 — handed to the child
    # the model's only output channel is `relay-send`: the worker, and the
    # runner it starts, must be able to find it however relay was launched
    from relay.cli.entrypoints import env_with_entrypoints

    proc = subprocess.Popen(
        [sys.executable, "-m", "relay.workers.run",
         "--swarm", swarm, "--role", role, "--project", str(project),
         "--state-root", str(state_root())],
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True, env=env_with_entrypoints(),
    )
    pf.write_text(str(proc.pid))
    return proc.pid


def stop_worker(swarm: str, role: str, timeout_s: float = 10.0) -> bool:
    """Returns True if the worker is gone (or was never running)."""
    reap_orphans(swarm, role,
                 keep=int(pidfile(swarm, role).read_text()) if pidfile(swarm, role).exists() else None)
    pf = pidfile(swarm, role)
    if not pf.exists():
        return True
    pid = int(pf.read_text())
    if is_running(pid):
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not is_running(pid):
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
    pf.unlink(missing_ok=True)
    return True


def reap_swarm(swarm: str) -> list[int]:
    """Kill EVERY worker process of a swarm, tracked or not (used by down)."""
    reaped = []
    for pid, s, _role in _worker_processes():
        if s == swarm:
            try:
                os.kill(pid, signal.SIGTERM)
                reaped.append(pid)
            except (ProcessLookupError, PermissionError):
                pass
    return reaped


def running_roles(swarm: str) -> dict[str, int]:
    out: dict[str, int] = {}
    if run_dir(swarm).is_dir():
        for pf in run_dir(swarm).glob("*.pid"):
            pid = int(pf.read_text())
            if is_running(pid):
                out[pf.stem] = pid
    return out
