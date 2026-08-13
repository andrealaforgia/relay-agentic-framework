from pathlib import Path

from relay.cli import procs
from relay.cli.watch import LogTails


def test_log_tails_attach_at_end_then_stream_increments(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RELAY_STATE_ROOT", str(tmp_path))
    log_dir = procs.log_dir("testswarm")
    log_dir.mkdir(parents=True)
    builder_log = log_dir / "builder.log"
    builder_log.write_text("old line from before watch started\n")

    tails = LogTails("testswarm")
    assert tails.read_new() == []  # attaches at the end: history is not replayed

    with builder_log.open("a") as f:
        f.write("[10:00:01] handling build.requested (01ABC)\n")
        f.write("[10:00:05]   $ uv run pytest -q tests/acceptance\n")
    (log_dir / "analyst.log").write_text("")  # new file appears mid-watch

    lines = tails.read_new()
    assert ("builder", "[10:00:01] handling build.requested (01ABC)") in lines
    assert any("uv run pytest" in text for _r, text in lines)

    with (log_dir / "analyst.log").open("a") as f:
        f.write("[10:00:07] handling analysis.requested (01DEF)\n")
    lines = tails.read_new()
    assert lines == [("analyst", "[10:00:07] handling analysis.requested (01DEF)")]
    assert tails.read_new() == []  # nothing new, nothing repeated
