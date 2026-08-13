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


def test_config_lives_under_dot_relay_with_legacy_fallback(tmp_path: Path) -> None:
    from relay.cli.context import config_path, has_config

    modern = tmp_path / "modern"
    (modern / ".relay").mkdir(parents=True)
    (modern / ".relay" / "relay.toml").write_text('[swarm]\nname = "m"\n')
    assert has_config(modern)
    assert config_path(modern) == modern / ".relay" / "relay.toml"
    assert find_project(modern / ".relay") == modern
    assert swarm_name(modern) == "m"

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "relay.toml").write_text('[swarm]\nname = "l"\n')
    assert config_path(legacy) == legacy / "relay.toml"
    assert swarm_name(legacy) == "l"


def test_wipe_swarm_keys_only_touches_that_swarm() -> None:
    import fakeredis

    from relay.cli.redisctl import wipe_swarm_keys

    client = fakeredis.FakeRedis(decode_responses=True)
    client.set("relay:doomed:seq", 5)
    client.xadd("relay:doomed:ledger", {"a": "b"})
    client.set("relay:doomed:project", "/x")
    client.set("relay:doomed-2:seq", 9)      # similar prefix, different swarm
    client.set("relay:survivor:seq", 1)

    assert wipe_swarm_keys(client, "doomed") == 3
    assert client.keys("relay:doomed:*") == []
    assert client.get("relay:doomed-2:seq") == "9"
    assert client.get("relay:survivor:seq") == "1"
