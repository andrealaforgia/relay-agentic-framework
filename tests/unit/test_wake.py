"""Waking an idle Interpreter: what we notice, when we knock, and the pump.

The nudge is a doorbell, never the delivery. It carries no content, so a
missed nudge costs nothing — the mail stays queued for the next keystroke or
the next Stop hook — and relay-inbox keeps sole ownership of the ack.
"""

import time

from relay.bus import groups
from relay.bus.keys import group_name, ledger_key
from relay.cli import pty_proxy
from relay.cli.wake import (
    NUDGE_COOLDOWN_S,
    QUIET_BEFORE_NUDGE_S,
    nudge_text,
    should_nudge,
    undelivered_for_interpreter,
)


def _group(client):
    groups.ensure_group(client, ledger_key("testswarm"), group_name("interpreter"))


def test_mail_for_the_interpreter_is_noticed(client, publisher) -> None:
    _group(client)
    publisher.send("analyst", "interpreter", "questions.raised",
                   {"question_id": "q-01J5AB3CDEF4GH5JK6MN7PQ8RS", "questions": ["Which physics?"]})
    assert len(undelivered_for_interpreter(client, "testswarm")) == 1


def test_the_swarms_own_chatter_is_not_mail(client, publisher) -> None:
    _group(client)
    publisher.send("builder", "coordinator", "error.raised", {"kind": "other", "detail": "x"})
    publisher.send("interpreter", "analyst", "analysis.requested", {"problem": "a sand game"})
    assert undelivered_for_interpreter(client, "testswarm") == []


def test_what_the_session_already_took_is_not_waiting(client, publisher) -> None:
    _group(client)
    publisher.send("analyst", "interpreter", "questions.raised",
                   {"question_id": "q-01J5AB3CDEF4GH5JK6MN7PQ8RS", "questions": ["Which physics?"]})
    groups.read_new(client, ledger_key("testswarm"), group_name("interpreter"),
                    "interpreter-native", block_ms=1)
    assert undelivered_for_interpreter(client, "testswarm") == []


def test_we_do_not_type_over_the_owner_mid_sentence() -> None:
    mail = ["01J5AB3CDEF4GH5JK6MN7PQ8RS"]
    assert should_nudge(waiting=mail, quiet_for_s=0.2, since_last_nudge_s=999) is False
    assert should_nudge(waiting=mail, quiet_for_s=QUIET_BEFORE_NUDGE_S,
                        since_last_nudge_s=999) is True


def test_we_do_not_ring_twice_in_a_row() -> None:
    mail = ["01J5AB3CDEF4GH5JK6MN7PQ8RS"]
    assert should_nudge(waiting=mail, quiet_for_s=99,
                        since_last_nudge_s=NUDGE_COOLDOWN_S - 1) is False


def test_no_mail_no_knock() -> None:
    assert should_nudge(waiting=[], quiet_for_s=99, since_last_nudge_s=999) is False


def test_the_nudge_carries_no_content_and_is_marked_synthetic() -> None:
    text = nudge_text("sandtris")
    assert text.startswith("<relay-wake>")          # the hook must not log it as the Owner's
    assert "relay-inbox --swarm sandtris" in text
    assert len(text) < 200                          # a knock, not the mail


def test_the_proxy_relays_a_programs_output(capfdbinary) -> None:
    code = pty_proxy.run(["/bin/sh", "-c", "printf hello-from-the-child"], dict(os_environ()))
    out, _ = capfdbinary.readouterr()
    assert b"hello-from-the-child" in out
    assert code == 0


def test_the_proxy_types_what_the_watcher_returns(capfdbinary) -> None:
    """The child reads a line and echoes it back: proof the nudge reaches the
    program's stdin exactly as a keystroke would."""
    knocked = {"done": False}

    def knock(quiet_for_s: float) -> str | None:
        if knocked["done"]:
            return None
        knocked["done"] = True
        return "<relay-wake> knock"

    pty_proxy.run(["/bin/sh", "-c", "read line; printf 'got:%s' \"$line\""],
                  dict(os_environ()), on_idle=knock, max_seconds=10)
    out, _ = capfdbinary.readouterr()
    assert b"got:<relay-wake> knock" in out


def os_environ():
    import os
    return os.environ


def test_the_proxy_gives_up_rather_than_hanging_forever(capfdbinary) -> None:
    started = time.monotonic()
    pty_proxy.run(["/bin/sh", "-c", "sleep 30"], dict(os_environ()), max_seconds=1.0)
    assert time.monotonic() - started < 20
