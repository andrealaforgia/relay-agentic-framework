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
MISSING_DEPENDENCY = "missing_dependency"  # the suite's own imports never resolved
SETUP_FAILED = "setup_failed"       # the worktree bootstrap (npm ci, uv sync) died

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

# The runner started but an import never resolved. This alone is NOT a fault:
# an honest TDD red often dies on exactly this line, because the module that
# does not resolve is the production code nobody has written yet. The
# discriminator is the project's own manifest: a missing import that the
# manifest DECLARES is an environment that was never bootstrapped (twelve
# playwright runs in a node_modules-less worktree once read as twelve red
# tests); a missing import the manifest has never heard of is the code under
# test, and the exit code is evidence. Relative paths are always code. The
# cap is larger than the spawn cap because these runtimes print a resolution
# stack (~1.4k chars for node) as their dying breath — while a suite that
# actually ran prints far more than 2k.
import re as _re

_MISSING_IMPORT = _re.compile(
    r"cannot find (?:package|module) '([^']+)'"
    r"|no module named '?([A-Za-z0-9_.\-]+)'?",
    _re.IGNORECASE,
)
MAX_DEPENDENCY_OUTPUT = 2000


def _declared_dependencies(workspace: object) -> set[str]:
    """Names the project's manifests promise exist, normalized (lower, -≡_)."""
    from pathlib import Path

    root = Path(str(workspace))
    names: set[str] = set()
    pkg = root / "package.json"
    if pkg.is_file():
        import json

        try:
            data = json.loads(pkg.read_text())
        except (ValueError, OSError):
            data = {}
        if isinstance(data, dict):
            for key in ("dependencies", "devDependencies",
                        "peerDependencies", "optionalDependencies"):
                section = data.get(key)
                if isinstance(section, dict):
                    names.update(section.keys())
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        import tomllib

        try:
            data = tomllib.loads(pyproject.read_text())
        except (tomllib.TOMLDecodeError, OSError):
            data = {}
        specs: list[str] = list((data.get("project") or {}).get("dependencies") or [])
        for group in ((data.get("dependency-groups") or {}).values()):
            specs.extend(s for s in group if isinstance(s, str))
        for spec in specs:
            name = _re.split(r"[\s\[<>=!~;(]", spec.strip(), maxsplit=1)[0]
            if name:
                names.add(name)
    return {n.lower().replace("-", "_") for n in names}


def _missing_declared_import(output: str, workspace: object) -> bool:
    match = _MISSING_IMPORT.search(output)
    if match is None:
        return False
    name = next(g for g in match.groups() if g)
    if name.startswith((".", "/")):
        return False                      # a project file: always code evidence
    top = name.split("/")[0] if not name.startswith("@") else "/".join(name.split("/")[:2])
    top = top.split(".")[0]               # python packages: compare the root module
    return top.lower().replace("-", "_") in _declared_dependencies(workspace)


def classify(exit_code: int, output: str, workspace: object | None = None) -> str | None:
    """The fault this run represents, or None when the exit code is evidence.

    Conservative on purpose: when in doubt this returns None, because calling
    a real failure a fault would stall a swarm that should be doing rework.
    `workspace` is the checkout the command ran in; without it, dependency
    faults are never claimed (the manifest is the only honest discriminator
    between "environment never bootstrapped" and "module not written yet").
    """
    if exit_code == 0:
        return None
    if exit_code in (126, 127):
        return NOT_EXECUTABLE
    lowered = output.lower()
    if len(output) <= MAX_SIGNATURE_OUTPUT \
            and any(signature in lowered for signature in SPAWN_SIGNATURES):
        return NOT_EXECUTABLE
    if workspace is not None and len(output) <= MAX_DEPENDENCY_OUTPUT \
            and _missing_declared_import(output, workspace):
        return MISSING_DEPENDENCY
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
