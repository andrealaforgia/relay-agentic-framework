"""Telling "the command did not run" from "the test failed".

A non-zero exit code is the only thing the coordinator's red/green machinery
reasons about, so a command that never started is indistinguishable from a
failing assertion. That is how a red verification once passed on a test that
was never executed: a toolgate holding `uv run pytest` ran it against a Rust
crate, every acceptance run "failed" exactly as red-verification wanted, and
two behaviours then blocked with "acceptance test still failing after build"
while `cargo test` was green the whole time.

An infrastructure fault is not evidence about the code. Naming it is what lets
the coordinator refuse to reason from it.

There is deliberately no `shutil.which` preflight here. The toolgate runs
commands through a shell whose PATH may not be this process's PATH, so a
which() check would invent faults for programs the shell can find perfectly
well. The shell already reports the truth as 127, and this module reads it.
"""

from __future__ import annotations

# Fault names — the `fault` field of run.completed.
NOT_EXECUTABLE = "not_executable"   # the command, or something it launched, is not there
NO_COMMAND = "no_command"           # nothing is configured for this run kind
MISSING_COMMIT = "missing_commit"   # the sha to pin the worktree to is absent
TIMEOUT = "timeout"                 # the command ran, but never finished

# Launchers that find their own subprocess and fail in their own words with
# their own exit code, so the shell's 126/127 never reaches us: `uv run pytest`
# exits 2 when pytest is missing. Matched only against SHORT output, because a
# suite that actually ran prints a summary, and one that never started prints
# a line or two. A long output that merely mentions these words is evidence.
SPAWN_SIGNATURES = (
    "failed to spawn:",                                     # uv
    "command not found",                                    # sh, bash, zsh
    "no such file or directory (os error 2)",               # rust std, uv
    "is not recognized as an internal or external command",  # cmd.exe
    "executable file not found",                            # docker
    "cannot execute:",                                      # busybox sh
)
MAX_SIGNATURE_OUTPUT = 500


def classify(exit_code: int, output: str) -> str | None:
    """The fault this run represents, or None when the exit code is evidence.

    Conservative on purpose: when in doubt this returns None, because calling
    a real failure a fault would stall a swarm that should be doing rework.
    """
    if exit_code == 0:
        return None
    if exit_code in (126, 127):
        return NOT_EXECUTABLE
    if len(output) <= MAX_SIGNATURE_OUTPUT:
        lowered = output.lower()
        if any(signature in lowered for signature in SPAWN_SIGNATURES):
            return NOT_EXECUTABLE
    return None


def merge_path(current: str, extra: str) -> str:
    """`current` with every directory of `extra` it does not already have.

    Order is preserved and the process's own PATH keeps precedence: the point
    is to reach tools the human's terminal can see, not to let a login profile
    shadow whatever the swarm was deliberately started with.
    """
    seen = [part for part in current.split(":") if part]
    known = set(seen)
    for part in extra.split(":"):
        if part and part not in known:
            seen.append(part)
            known.add(part)
    return ":".join(seen)


def login_shell_path(timeout_s: float = 10.0) -> str | None:
    """The PATH a human gets in a terminal on this machine, or None.

    Workers are started detached, so they inherit whatever environment the
    launcher happened to have — which is how `cargo` at ~/.cargo/bin can be
    invisible to the toolgate while being perfectly ordinary in a shell. This
    is read ONCE, at worker start; commands are not run through a login shell,
    because a profile's banners would end up in the run's captured output and
    then in the evidence.
    """
    import os
    import subprocess

    shell = os.environ.get("SHELL")
    if not shell:
        return None
    try:
        proc = subprocess.run(
            [shell, "-lc", 'printf %s "$PATH"'],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None
