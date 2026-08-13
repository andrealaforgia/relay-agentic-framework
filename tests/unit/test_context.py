from pathlib import Path

import pytest

from relay.cli.context import NoProjectError, find_project, resolve_swarm, swarm_name


def _write_config(project: Path, name: str | None = None) -> None:
    body = f'[swarm]\nname = "{name}"\n' if name else "[commands]\n"
    (project / "relay.toml").write_text(body)


def test_find_project_walks_up(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    nested = project / "src" / "deep"
    nested.mkdir(parents=True)
    _write_config(project, "acme")
    assert find_project(nested) == project


def test_no_project_raises(tmp_path: Path) -> None:
    with pytest.raises(NoProjectError):
        find_project(tmp_path)


def test_swarm_name_from_config_else_folder(tmp_path: Path) -> None:
    named = tmp_path / "named"
    named.mkdir()
    _write_config(named, "acme")
    assert swarm_name(named) == "acme"

    bare = tmp_path / "bare"
    bare.mkdir()
    _write_config(bare)
    assert swarm_name(bare) == "bare"


def test_explicit_swarm_wins(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _write_config(project, "acme")
    assert resolve_swarm("other", project) == "other"
    assert resolve_swarm(None, project) == "acme"
