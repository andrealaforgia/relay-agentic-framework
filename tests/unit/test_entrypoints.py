"""relay-send and relay-inbox must be findable by everything we start.

The failure this guards against: ~/.local/bin exported from ~/.zshrc, which
login non-interactive shells (`zsh -lc`, what the window launcher and hooks
get) never read. The commands are installed, on PATH when you type, and
missing exactly where the swarm needs them.
"""

import json
import os
from pathlib import Path

from relay.cli import entrypoints
from relay.cli.profiles import write_profiles


def _fake_install(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in entrypoints.COMMANDS:
        script = bindir / name
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
    return bindir


def test_the_directory_is_found_through_path(tmp_path, monkeypatch) -> None:
    bindir = _fake_install(tmp_path)
    monkeypatch.setenv("PATH", str(bindir))
    assert entrypoints.entrypoint_dir() == bindir


def test_the_directory_is_found_even_when_path_is_bare(tmp_path, monkeypatch) -> None:
    """The login-shell case: nothing on PATH, but we know where we came from."""
    bindir = _fake_install(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr("sys.argv", [str(bindir / "relay"), "up"])
    assert entrypoints.entrypoint_dir() == bindir


def test_no_installation_is_not_a_crash(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr("sys.argv", [str(tmp_path / "nowhere" / "relay")])
    monkeypatch.setattr("sys.executable", str(tmp_path / "nowhere" / "python"))
    assert entrypoints.entrypoint_dir() is None
    assert entrypoints.relay_command("relay-send") == "relay-send"


def test_commands_written_into_files_are_absolute(tmp_path, monkeypatch) -> None:
    bindir = _fake_install(tmp_path)
    monkeypatch.setenv("PATH", str(bindir))
    assert entrypoints.relay_command("relay-inbox") == str(bindir / "relay-inbox")


def test_spawned_environments_carry_the_directory(tmp_path, monkeypatch) -> None:
    bindir = _fake_install(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr("sys.argv", [str(bindir / "relay")])

    env = entrypoints.env_with_entrypoints()
    assert env["PATH"].split(os.pathsep)[0] == str(bindir)
    assert "/usr/bin" in env["PATH"]                       # nothing is lost
    assert entrypoints.env_with_entrypoints(env) == env    # idempotent


def test_the_interpreter_hooks_do_not_depend_on_path(tmp_path, monkeypatch) -> None:
    """The reported failure: `/bin/sh: relay-inbox: command not found`."""
    bindir = _fake_install(tmp_path)
    monkeypatch.setenv("PATH", str(bindir))
    project = tmp_path / "project"
    project.mkdir()

    write_profiles(project, "acme")
    settings = json.loads((project / ".relay" / "settings" / "interpreter.json").read_text())
    commands = [h["command"]
                for entry in settings["hooks"].values()
                for hook in entry for h in hook["hooks"]]
    assert commands, "the interpreter profile must carry the relay-inbox hooks"
    for command in commands:
        assert command.startswith(str(bindir / "relay-inbox")), command
