import pytest
from typer.testing import CliRunner


def test_doctor_evidence_check_reports_missing_boundary(client, monkeypatch):
    from relay.cli import main

    monkeypatch.setattr(main, "get_client", lambda: client)
    monkeypatch.setattr(client, "config_get", lambda *_: {"appendonly": "yes"})
    monkeypatch.setattr(main, "_check_toolchain", lambda: 0)
    monkeypatch.delenv("RELAY_EVIDENCE_IMAGE", raising=False)
    monkeypatch.delenv("RELAY_EVIDENCE_ALLOW_NATIVE", raising=False)
    result = CliRunner().invoke(
        main.app, ["doctor", "--swarm", "testswarm", "--evidence"]
    )
    assert result.exit_code == 1
    assert "RELAY_EVIDENCE_IMAGE" in result.output


@pytest.mark.parametrize(
    "image,native,failures",
    [
        ("", "", 1),
        ("python:latest", "", 1),
        ("", "1", 0),
        ("python@sha256:" + "a" * 64, "", 0),
    ],
)
def test_preflight_checks_configuration_without_running_code(
    monkeypatch, image, native, failures
):
    from relay.cli.main import _check_evidence_configuration

    monkeypatch.setenv("RELAY_EVIDENCE_IMAGE", image)
    monkeypatch.setenv("RELAY_EVIDENCE_ALLOW_NATIVE", native)
    assert _check_evidence_configuration() == failures
