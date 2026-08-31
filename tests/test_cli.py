"""
Whole-process CLI tests against a stub device.

These cover behaviour that only exists in a real process: how long a mode
runs, what it does when its consumer disappears, and what it leaves behind
when the system terminates it.
"""

import subprocess
import sys
import time
from pathlib import Path

RUNNER = Path(__file__).parent / "_stub_run.py"


def run(*argv, **kwargs):
    return subprocess.run(
        [sys.executable, str(RUNNER), *argv],
        capture_output=True,
        text=True,
        **kwargs,
    )


def test_dump_keeps_going_until_it_is_stopped():
    """
    --dump says "Ctrl-C to stop" and is the tool users are sent to when a
    reading looks wrong; it must not quietly stop on its own mid-capture.
    """
    process = subprocess.Popen(
        [sys.executable, str(RUNNER), "--dump"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(5)
        assert process.poll() is None, "--dump exited on its own"
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_broken_pipe_is_not_a_traceback():
    """The README's own pipeline ends when its consumer does."""
    reader = subprocess.Popen(
        [sys.executable, str(RUNNER)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert reader.stdout.readline(), "expected at least one record"
    reader.stdout.close()
    _, errors = reader.communicate(timeout=15)
    assert b"BrokenPipeError" not in errors, errors.decode(errors="replace")
    assert b"Traceback" not in errors, errors.decode(errors="replace")


def test_help_lists_the_options():
    result = run("--help")
    assert result.returncode == 0
    assert "--metric" in result.stdout
