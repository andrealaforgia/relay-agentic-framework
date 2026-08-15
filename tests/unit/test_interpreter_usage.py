"""The Interpreter bills too — and it is the one session nothing bounds.

It runs as a native Claude Code session on opus, resumed across the whole
engagement, so its context (and its price per turn) only grows. It cannot
publish usage from inside a worker loop because it has none; the Stop hook
reads what the session just spent from its own transcript and puts it on the
ledger, so the priciest role is not the invisible one.
"""

import json

from relay.cli.session_usage import read_new_usage, record_usage

MODEL = "claude-opus-5"


def _line(cache_read: int, output: int, model: str = MODEL) -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"model": model, "usage": {
            "input_tokens": 4, "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": cache_read, "output_tokens": output,
        }},
    })


def _transcript(tmp_path, *lines: str):
    path = tmp_path / "session.jsonl"
    path.write_text("".join(line + "\n" for line in lines))
    return path


def test_reads_only_what_is_new_since_the_last_look(tmp_path) -> None:
    transcript = _transcript(tmp_path, _line(1000, 10), _line(2000, 20))
    state = tmp_path / "usage.json"

    first = read_new_usage(transcript, state)
    assert first is not None
    assert first.usage["cache_read_input_tokens"] == 3000
    assert first.assistant_messages == 2
    assert first.model == MODEL
    assert first.fresh is True

    record_usage(state, transcript, first)
    transcript.write_text(transcript.read_text() + _line(5000, 50) + "\n")

    second = read_new_usage(transcript, state)
    assert second is not None
    assert second.usage["cache_read_input_tokens"] == 5000   # not the earlier 3000
    assert second.assistant_messages == 1
    assert second.fresh is False
    assert second.session_turn == 2


def test_nothing_new_is_reported_as_nothing(tmp_path) -> None:
    transcript = _transcript(tmp_path, _line(1000, 10))
    state = tmp_path / "usage.json"
    record_usage(state, transcript, read_new_usage(transcript, state))
    assert read_new_usage(transcript, state) is None


def test_a_new_transcript_starts_from_scratch(tmp_path) -> None:
    state = tmp_path / "usage.json"
    first = _transcript(tmp_path, _line(1000, 10))
    record_usage(state, first, read_new_usage(first, state))

    other = tmp_path / "other.jsonl"
    other.write_text(_line(7000, 70) + "\n")
    slice_ = read_new_usage(other, state)
    assert slice_ is not None
    assert slice_.usage["cache_read_input_tokens"] == 7000
    assert slice_.fresh is True


def test_a_missing_or_broken_transcript_is_not_an_error(tmp_path) -> None:
    state = tmp_path / "usage.json"
    assert read_new_usage(tmp_path / "gone.jsonl", state) is None
    broken = tmp_path / "broken.jsonl"
    broken.write_text("not json\n" + _line(500, 5) + "\n")
    slice_ = read_new_usage(broken, state)
    assert slice_ is not None and slice_.usage["cache_read_input_tokens"] == 500


def test_the_priciest_model_labels_a_mixed_batch(tmp_path) -> None:
    transcript = _transcript(
        tmp_path, _line(1000, 10, model="claude-haiku-4-5"), _line(9000, 90, model=MODEL)
    )
    slice_ = read_new_usage(transcript, tmp_path / "usage.json")
    assert slice_ is not None
    assert slice_.model == MODEL          # the one that moved the bill


def test_the_stop_hook_puts_the_interpreters_spend_on_the_ledger(
    tmp_path, client, monkeypatch, capsys
) -> None:
    import relay.cli.inbox as inbox
    from relay.ledger.usage import fold_usage

    transcript = _transcript(tmp_path, _line(120_000, 900))
    monkeypatch.setattr(inbox, "get_client", lambda: client)
    monkeypatch.setenv("RELAY_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({"transcript_path": str(transcript)})))
    monkeypatch.setattr("sys.argv", ["relay-inbox", "--swarm", "testswarm", "--hook-stop"])

    assert inbox.main() == 0

    report = fold_usage(client, "testswarm")
    assert report.by_role["interpreter"]["cache_read_input_tokens"] == 120_000
    assert report.by_model[MODEL]["turns"] == 1


class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text
