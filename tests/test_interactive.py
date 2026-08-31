"""
Interactive tests for the live readout.

The reader only enters cbreak mode and accepts keys when stdin is a terminal,
so these drive it through a pty. That covers the tare, reset and unit keys, the
exit path, and whether the terminal is left as it was found -- none of which a
pipe or a direct call reaches.
"""

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import read_scale as rs

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="pty is POSIX-only; Windows uses the msvcrt key reader"
)

pty = pytest.importorskip("pty")
termios = pytest.importorskip("termios")

CHILD = Path(__file__).parent / "_pty_child.py"
STARTUP_TIMEOUT = 10.0
RESPONSE_TIMEOUT = 5.0

# The stub reports this many counts above its zero; the expected readings are
# derived so a change to the calibration constant does not break the tests.
LOAD_COUNTS = 1453
LOADED_IMPERIAL = rs.format_weight_display(LOAD_COUNTS * rs.DEFAULT_CALIBRATION, False)
LOADED_METRIC = rs.format_weight_display(LOAD_COUNTS * rs.DEFAULT_CALIBRATION, True)


class ReaderSession:
    """A read_scale live readout running on the far end of a pty."""

    def __init__(self):
        self.master, slave = pty.openpty()
        self.process = subprocess.Popen(
            [sys.executable, str(CHILD)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
        os.close(slave)
        self.output = bytearray()

    def _drain(self):
        os.set_blocking(self.master, False)
        try:
            chunk = os.read(self.master, 4096)
        except (BlockingIOError, OSError):
            return
        if chunk:
            self.output.extend(chunk)

    @property
    def text(self):
        return bytes(self.output).decode(errors="replace")

    def wait_for(self, pattern, timeout=RESPONSE_TIMEOUT):
        """Block until pattern appears in the output, and return the full text."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._drain()
            if re.search(pattern, self.text):
                return self.text
            if self.process.poll() is not None:
                self._drain()
                raise AssertionError(
                    f"reader exited with {self.process.returncode} before "
                    f"{pattern!r} appeared; output:\n{self.text!r}"
                )
            time.sleep(0.02)
        raise AssertionError(f"never saw {pattern!r} in:\n{self.text!r}")

    def press(self, key, expect, timeout=RESPONSE_TIMEOUT):
        """
        Send a key and wait for the readout to show `expect`.

        Waiting for the expected reading rather than the next one to arrive
        keeps this deterministic: the readout streams continuously, so a frame
        rendered just before the key was handled would otherwise be read as
        the response to it.
        """
        mark = len(self.output)
        os.write(self.master, key)
        deadline = time.monotonic() + timeout
        seen = []
        while time.monotonic() < deadline:
            self._drain()
            seen = weights_in(bytes(self.output[mark:]).decode(errors="replace"))
            if expect in seen:
                return
            if self.process.poll() is not None:
                raise AssertionError(f"reader exited with {self.process.returncode} after {key!r}")
            time.sleep(0.02)
        raise AssertionError(f"after {key!r} never saw {expect!r}; saw {seen}")

    def wait_for_exit(self, timeout=RESPONSE_TIMEOUT):
        """
        Wait for the reader to exit, draining as it goes.

        The readout keeps writing until it stops, so a caller that blocks
        without reading fills the pty buffer and stalls the child on write.
        A real terminal is always being drained; this reproduces that.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._drain()
            code = self.process.poll()
            if code is not None:
                return code
            time.sleep(0.02)
        raise AssertionError(f"reader did not exit within {timeout}s")

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        os.close(self.master)


def weights_in(text):
    """Every weight the readout printed, in order."""
    return [m.strip() for m in re.findall(r"Weight: ([^\r\n\x1b]+)", text)]


def last_weight(text):
    found = weights_in(text)
    assert found, f"no weight in output: {text!r}"
    return found[-1]


@pytest.fixture
def reader():
    session = ReaderSession()
    try:
        session.wait_for(r"Weight:", timeout=STARTUP_TIMEOUT)
        yield session
    finally:
        session.close()


def test_shows_the_key_legend_on_startup(reader):
    assert "T: Tare" in reader.text


def test_reports_the_load_in_imperial_by_default(reader):
    assert last_weight(reader.wait_for(re.escape(LOADED_IMPERIAL))) == LOADED_IMPERIAL


def test_m_switches_to_metric(reader):
    reader.wait_for(re.escape(LOADED_IMPERIAL))
    reader.press(b"M", LOADED_METRIC)


def test_t_tares_the_current_load_to_zero(reader):
    reader.wait_for(re.escape(LOADED_IMPERIAL))
    reader.press(b"T", "0.00 oz")


def test_r_restores_the_startup_zero(reader):
    reader.wait_for(re.escape(LOADED_IMPERIAL))
    reader.press(b"T", "0.00 oz")
    reader.press(b"R", LOADED_IMPERIAL)


def test_readout_erases_to_end_of_line(reader):
    """Without this a shorter reading leaves fragments of a longer one behind."""
    reader.wait_for(re.escape(LOADED_IMPERIAL))
    assert "\x1b[K" in reader.text


def test_escape_exits_cleanly(reader):
    os.write(reader.master, b"\x1b")
    assert reader.wait_for_exit() == 0


def test_terminal_is_restored_on_exit(reader):
    os.write(reader.master, b"\x1b")
    reader.wait_for_exit()
    attrs = termios.tcgetattr(reader.master)
    assert attrs[3] & termios.ECHO, "echo left disabled"
    assert attrs[3] & termios.ICANON, "canonical mode left disabled"


def test_arrow_keys_do_not_quit(reader):
    """
    Every arrow, Home/End and function key starts with ESC, so a naive
    check for ESC ends the session on a stray cursor press.
    """
    reader.wait_for(re.escape(LOADED_IMPERIAL))
    os.write(reader.master, b"\x1b[A")  # up arrow
    os.write(reader.master, b"\x1b[B")  # down arrow
    time.sleep(0.6)
    reader._drain()
    assert reader.process.poll() is None, "an arrow key ended the readout"
    reader.press(b"M", LOADED_METRIC), "and the reader still accepts keys"


def test_a_burst_of_keys_is_not_swallowed(reader):
    """
    Two keys pressed together must both act. Reading through a buffered
    stream while polling the descriptor strands everything after the first.
    """
    reader.wait_for(re.escape(LOADED_IMPERIAL))
    os.write(reader.master, b"MT")  # metric, then tare
    deadline = time.monotonic() + RESPONSE_TIMEOUT
    while time.monotonic() < deadline:
        reader._drain()
        if rs.format_weight_display(0.0, True) in weights_in(reader.text):
            return
        time.sleep(0.02)
    raise AssertionError(f"second key of the burst was dropped; saw {weights_in(reader.text)[-6:]}")


def test_interrupt_reports_the_documented_exit_code(reader):
    """
    The README documents 130 for an interrupt; every mode must agree.

    The signal is sent directly rather than as a Ctrl-C byte: the child is
    not this pty's foreground process group, so the line discipline would
    swallow the byte without delivering anything.
    """
    reader.wait_for(re.escape(LOADED_IMPERIAL))
    reader.process.send_signal(signal.SIGINT)
    assert reader.wait_for_exit() == rs.EXIT_INTERRUPTED


TERMINATING_SIGNALS = [
    getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)
]


@pytest.mark.parametrize("signum", TERMINATING_SIGNALS)
def test_termination_signals_restore_the_terminal(reader, signum):
    """
    A closed window, a kill, or a service manager stopping the reader must
    not leave the terminal without echo or line editing.
    """
    reader.wait_for(re.escape(LOADED_IMPERIAL))
    reader.process.send_signal(signum)
    reader.wait_for_exit()
    attrs = termios.tcgetattr(reader.master)
    assert attrs[3] & termios.ECHO, "echo left disabled"
    assert attrs[3] & termios.ICANON, "canonical mode left disabled"
