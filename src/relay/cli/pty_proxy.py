"""Run a program under a pseudo-terminal and keep hold of its input.

`relay chat` used to exec Claude Code and disappear, handing the terminal
straight over. That is why an idle Interpreter could not be woken: nobody
owned the keyboard any more. Here relay stays in the middle, relaying bytes
untouched — you get the real Claude Code interface — while holding the write
end of the session's input for its whole life.

This is v1's instinct with v1's mistakes removed. We do not borrow a window
we did not create (relay forks the session itself), and we never read the
screen to decide anything: the only trigger is a ledger event, and bytes
flowing out are copied, never parsed.
"""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import time
import tty
from collections.abc import Callable
from types import FrameType

POLL_S = 0.5
# Typed text arriving in one burst reads as a paste, and a paste's Enter goes
# into the input box instead of submitting it. So pause before the return, and
# send a second one: on an empty box the extra return is a no-op, and if the
# first was swallowed as a newline the second submits.
TYPE_SETTLE_S = 0.25


def _keyboard_fd() -> int | None:
    """Our stdin, when there is a real one. Absent under a test harness or a
    pipe, in which case the session simply has no keyboard to relay."""
    try:
        return sys.stdin.fileno()
    except (AttributeError, ValueError, OSError):
        return None


def _propagate_winsize(master_fd: int, keyboard: int | None) -> None:
    if keyboard is None:
        return
    try:
        size = fcntl.ioctl(keyboard, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def _type(master_fd: int, text: str) -> None:
    """Type into the session the way a person does: the words, a beat, then
    return. An empty string is a bare return, which is how a swallowed one is
    retried without re-typing the message."""
    if text:
        os.write(master_fd, text.encode())
        time.sleep(TYPE_SETTLE_S)
    os.write(master_fd, b"\r")


def run(
    argv: list[str],
    env: dict[str, str],
    *,
    on_idle: Callable[[float], str | None] | None = None,
    max_seconds: float | None = None,
) -> int:
    """Run argv under a pty, relaying stdin/stdout. Returns its exit status.

    `on_idle(quiet_for_s)` is called between reads with how long the keyboard
    has been quiet; returning a string types it into the session, exactly as
    the Owner would have. It is the only way in — nothing here inspects what
    the program prints.
    """
    pid, master_fd = pty.fork()
    if pid == 0:                                    # child: become the program
        os.execvpe(argv[0], argv, env)
        os._exit(127)                               # unreachable except on failure

    keyboard = _keyboard_fd()
    _propagate_winsize(master_fd, keyboard)

    def _resized(_sig: int, _frame: FrameType | None) -> None:
        _propagate_winsize(master_fd, keyboard)

    signal.signal(signal.SIGWINCH, _resized)

    saved = None
    if keyboard is not None and os.isatty(keyboard):
        saved = termios.tcgetattr(keyboard)
        tty.setraw(keyboard)

    last_key = started = time.monotonic()
    try:
        while True:
            if max_seconds is not None and time.monotonic() - started > max_seconds:
                break
            watched = [master_fd] if keyboard is None else [keyboard, master_fd]
            try:
                readable, _, _ = select.select(watched, [], [], POLL_S)
            except OSError as e:
                if e.errno == errno.EINTR:          # a window resize, most often
                    continue
                raise

            if keyboard is not None and keyboard in readable:
                data = os.read(keyboard, 4096)
                if not data:
                    break
                last_key = time.monotonic()
                os.write(master_fd, data)

            if master_fd in readable:
                try:
                    out = os.read(master_fd, 65536)
                except OSError:
                    break                            # the program closed the pty
                if not out:
                    break
                os.write(sys.stdout.fileno(), out)

            if on_idle is not None:
                typed = on_idle(time.monotonic() - last_key)
                if typed is not None:
                    _type(master_fd, typed)
    finally:
        if saved is not None and keyboard is not None:
            termios.tcsetattr(keyboard, termios.TCSAFLUSH, saved)
        try:
            os.close(master_fd)
        except OSError:
            pass
        if max_seconds is not None:                 # bounded run: never orphan it
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status
