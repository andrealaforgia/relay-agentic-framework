"""Where this installation's relay commands live — and putting them on PATH.

`relay-send` is the ONLY output channel an assistant has, and `relay-inbox`
is how mail reaches the Interpreter's session. Both are found by name, by
processes we spawn: hooks (`/bin/sh -c`), viewer windows (`/bin/zsh -lc`),
and every model turn's Bash calls.

None of those read an interactive shell's rc file. A PATH entry exported from
~/.zshrc is invisible to all of them, so a perfectly installed relay can be
"command not found" at exactly the moment it matters — and a builder whose
`relay-send` is missing looks identical to a model that refused to answer.

So: resolve the directory once, absolutize what we write into settings files,
and export it into the environment of everything we start.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

COMMANDS = ("relay", "relay-send", "relay-id", "relay-inbox")


def entrypoint_dir() -> Path | None:
    """The directory holding this installation's relay commands, if findable.

    Symlinks are NOT resolved: ~/.local/bin (the link farm) is the directory
    worth putting on PATH, not uv's private tool store behind it.
    """
    found = shutil.which("relay-send") or shutil.which("relay")
    if found:
        return Path(found).parent
    # not on PATH (the login-shell case): fall back to how we were invoked,
    # then to the interpreter running us — both sit beside the scripts.
    for candidate in (Path(sys.argv[0]).resolve().parent, Path(sys.executable).parent):
        if (candidate / "relay-send").exists():
            return candidate
    return None


def relay_command(name: str) -> str:
    """Absolute path to a relay command when we can find one, else its name.

    Used for commands we write into files that other programs will run for us
    (Claude Code hooks), where PATH is not ours to control.
    """
    found = shutil.which(name)
    if found:
        return found
    directory = entrypoint_dir()
    if directory and (directory / name).exists():
        return str(directory / name)
    return name


def env_with_entrypoints(env: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment with the relay commands guaranteed on PATH."""
    result = dict(os.environ if env is None else env)
    directory = entrypoint_dir()
    if directory is None:
        return result
    path = result.get("PATH", "")
    if str(directory) not in path.split(os.pathsep):
        result["PATH"] = f"{directory}{os.pathsep}{path}" if path else str(directory)
    return result
