"""
Reading-loop tests driven by scripted devices.

These cover what the loops do when the scale stops behaving: reports that
never arrive, and reports too short to decode. Both are states the hardware
can enter and neither can be reproduced with a real scale on demand.
"""

import time

import pytest

import read_scale as rs

GOOD_RAW = 56527
HEADER = [0xAB, 0x12, 0x13, 0x81, 0x36, 0x0D]


def report(raw):
    return HEADER + [raw >> 8, raw & 0xFF]


class ScriptedDevice:
    """
    Answers with good reports, then with whatever failure is being tested.

    The default supplies exactly the samples measure_zero consumes, so the
    reading loop that follows it sees nothing usable at all.
    """

    def __init__(self, good_reports=rs.ZERO_SAMPLE_COUNT, then=None):
        self._good = good_reports
        self._then = [] if then is None else then
        self._reads = 0

    def read(self, size, timeout_ms=0):
        self._reads += 1
        if self._reads <= self._good:
            return report(GOOD_RAW)
        time.sleep(0.01)
        return list(self._then)

    def close(self):
        pass


@pytest.fixture
def quick_timeouts(monkeypatch):
    """Keep the silence watchdog short so tests finish promptly."""
    monkeypatch.setattr(rs, "SILENCE_TIMEOUT_S", 0.5)
    monkeypatch.setattr(rs, "FIRST_REPORT_TIMEOUT_S", 1.0)


def args_for(*argv):
    return rs.build_parser().parse_args(list(argv))


# ---------------------------------------------------------------------------
# The live loop must give up rather than sit silent forever
# ---------------------------------------------------------------------------


def test_live_gives_up_when_the_device_goes_quiet(quick_timeouts):
    device = ScriptedDevice(then=[])
    started = time.monotonic()
    assert rs.run_live(device, rs.Config(), args_for()) == rs.EXIT_DEVICE_ERROR
    assert time.monotonic() - started < 10, "must not hang waiting for a silent device"


def test_live_gives_up_when_reports_stay_undecodable(quick_timeouts):
    device = ScriptedDevice(then=[0x01, 0x02])
    started = time.monotonic()
    assert rs.run_live(device, rs.Config(), args_for()) == rs.EXIT_DEVICE_ERROR
    assert time.monotonic() - started < 10, "must not hang on undecodable reports"


class OverRangeThenBack:
    """Reports an impossible load, returns to normal, then goes over again."""

    def __init__(self):
        self._reads = 0

    def read(self, size, timeout_ms=0):
        self._reads += 1
        time.sleep(0.005)
        if self._reads <= rs.ZERO_SAMPLE_COUNT:
            raw = GOOD_RAW
        elif self._reads <= rs.ZERO_SAMPLE_COUNT + 20:
            raw = (GOOD_RAW + 30000) & 0xFFFF  # far over capacity
        elif self._reads <= rs.ZERO_SAMPLE_COUNT + 40:
            raw = GOOD_RAW  # back to a sane load
        elif self._reads <= rs.ZERO_SAMPLE_COUNT + 60:
            raw = (GOOD_RAW + 30000) & 0xFFFF  # over again
        else:
            raise OSError("done")
        return report(raw)

    def close(self):
        pass


def test_over_range_is_reported_each_time_it_recurs(quick_timeouts, caplog):
    """
    A second excursion must warn again. Warning once per process means a
    later over-capacity load is carried silently.
    """
    caplog.set_level("WARNING", logger="read_scale")
    rs.run_live(OverRangeThenBack(), rs.Config(), args_for())
    over_range = [r for r in caplog.records if "capacity" in r.getMessage()]
    assert len(over_range) >= 2, f"warned {len(over_range)} time(s); the second load was silent"
