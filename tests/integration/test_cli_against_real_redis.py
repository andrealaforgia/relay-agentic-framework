"""End-to-end against a real, throwaway redis-server: relay-send publishes,
relay audit/status/export see it. Skipped when redis-server is not installed.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

import relay.bus.client as bus_client
from relay.cli.main import app
from relay.cli.send import main as send_main

pytestmark = pytest.mark.skipif(
    shutil.which("redis-server") is None, reason="redis-server not installed"
)

runner = CliRunner()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def redis_server(tmp_path_factory):
    port = _free_port()
    datadir = tmp_path_factory.mktemp("redis")
    proc = subprocess.Popen(
        ["redis-server", "--port", str(port), "--appendonly", "yes", "--dir", str(datadir),
         "--save", "", "--bind", "127.0.0.1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    client = None
    for _ in range(100):
        try:
            client = bus_client.get_client(host="127.0.0.1", port=port)
            client.ping()
            break
        except Exception:
            time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("redis-server did not come up")
    yield port
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(autouse=True)
def _point_env_at_server(redis_server, monkeypatch):
    monkeypatch.setenv("REDIS_HOST", "127.0.0.1")
    monkeypatch.setenv("REDIS_PORT", str(redis_server))


def _send(argv: list[str]) -> int:
    import sys
    old = sys.argv
    sys.argv = ["relay-send", *argv]
    try:
        return send_main()
    finally:
        sys.argv = old


def test_send_audit_status_export_round_trip(tmp_path: Path, capsys) -> None:
    rc = _send([
        "--swarm", "clitest", "--from", "owner", "--to", "interpreter",
        "--type", "problem.stated", "--payload", json.dumps({"text": "a problem"}),
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["seq"] == 1

    result = runner.invoke(app, ["audit", "--swarm", "clitest"])
    assert result.exit_code == 0, result.output
    assert "no findings" in result.output

    result = runner.invoke(app, ["status", "--swarm", "clitest"])
    assert result.exit_code == 0
    assert "ledger: 1 entries" in result.output

    out_file = tmp_path / "ledger.jsonl"
    result = runner.invoke(app, ["export", "--swarm", "clitest", "--out", str(out_file)])
    assert result.exit_code == 0
    assert json.loads(out_file.read_text().strip())["type"] == "problem.stated"

    result = runner.invoke(app, ["doctor", "--swarm", "clitest"])
    assert result.exit_code == 0, result.output


def test_send_rejects_off_contract_loudly(capsys) -> None:
    rc = _send([
        "--swarm", "clitest", "--from", "builder", "--to", "owner",
        "--type", "update.shared", "--payload", json.dumps({"text": "hi"}),
    ])
    assert rc == 1
    assert "topology violation" in capsys.readouterr().err


def test_contract_gen_and_show() -> None:
    result = runner.invoke(app, ["contract", "show"])
    assert result.exit_code == 0
    assert "message types" in result.output


def test_swarm_name_belongs_to_one_project(tmp_path: Path) -> None:
    from relay.cli.main import _claim_swarm

    a, b = tmp_path / "x" / "myapp", tmp_path / "y" / "myapp"
    a.mkdir(parents=True), b.mkdir(parents=True)
    assert _claim_swarm("myapp", a) is True
    assert _claim_swarm("myapp", a) is True   # same project re-claims freely
    assert _claim_swarm("myapp", b) is False  # a different folder may not share the ledger


def test_inbox_drain_returns_immediately_on_real_redis(capsys, monkeypatch) -> None:
    """BLOCK 0 means 'forever' in real Redis (fakeredis hides this): a plain
    drain with an empty queue must return instantly, not hang."""
    import io
    import time as _time

    from relay.cli import inbox

    monkeypatch.setattr("sys.argv", ["relay-inbox", "--swarm", "drain-test"])
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    started = _time.monotonic()
    inbox.main()
    elapsed = _time.monotonic() - started
    assert elapsed < 2.0, f"drain blocked for {elapsed:.1f}s"
    assert "(no relay mail)" in capsys.readouterr().out
