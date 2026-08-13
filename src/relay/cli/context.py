"""Project/swarm resolution: relay commands work like git commands.

`relay up <folder>` records the swarm name in relay.toml; every other command
finds the project by walking up from the current directory and reads the name
from there. Explicit --swarm/--project always win (multi-swarm, cross-machine).
"""

from __future__ import annotations

import tomllib
from pathlib import Path


class NoProjectError(RuntimeError):
    def __init__(self, start: Path) -> None:
        super().__init__(
            f"no .relay/relay.toml found in {start} or any parent — "
            "run `relay up <folder>` first, or pass --swarm explicitly"
        )


def config_path(project: Path) -> Path:
    """The project's relay config: .relay/relay.toml (root relay.toml is the
    legacy location, still honored until `relay up` migrates it)."""
    preferred = project / ".relay" / "relay.toml"
    legacy = project / "relay.toml"
    return preferred if preferred.is_file() or not legacy.is_file() else legacy


def has_config(project: Path) -> bool:
    return (project / ".relay" / "relay.toml").is_file() or (project / "relay.toml").is_file()


def find_project(start: Path | None = None) -> Path:
    """Walk up from `start` (default cwd) to the nearest relay-configured directory."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if candidate.name == ".relay":
            continue  # its relay.toml belongs to the parent project
        if has_config(candidate):
            return candidate
    raise NoProjectError(current)


def swarm_name(project: Path) -> str:
    config = tomllib.loads(config_path(project).read_text())
    swarm_cfg = config.get("swarm")
    if isinstance(swarm_cfg, dict):
        name = swarm_cfg.get("name")
        if isinstance(name, str) and name:
            return name
    return project.name


def resolve_swarm(explicit: str | None, start: Path | None = None) -> str:
    """--swarm wins; otherwise the enclosing project's recorded name."""
    if explicit:
        return explicit
    return swarm_name(find_project(start))
