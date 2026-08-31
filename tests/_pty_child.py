"""
Entry point for the interactive tests: runs the live readout against a stub
device. Not collected by pytest; spawned by test_interactive.py under a pty.

The stub reports an empty platter while the startup zero is measured, then a
steady load, so tare and reset have something to act on.
"""

import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ZERO_RAW = 56527
LOAD_COUNTS = 1453
ZERO_SAMPLE_READS = 12


class _StubDevice:
    def __init__(self):
        self._reads = 0

    def open_path(self, path):
        pass

    def read(self, size, timeout_ms=0):
        time.sleep(0.02)
        self._reads += 1
        raw = ZERO_RAW if self._reads <= ZERO_SAMPLE_READS else ZERO_RAW + LOAD_COUNTS
        return [0xAB, 0x12, 0x13, 0x81, 0x36, 0x0D, raw >> 8, raw & 0xFF]

    def get_manufacturer_string(self):
        return "RadioShack"

    def get_product_string(self):
        return "USB Electronic Scale"

    def close(self):
        pass


def main():
    stub = types.ModuleType("hid")
    stub.device = _StubDevice
    stub.enumerate = lambda vid, pid: [{"path": b"stub", "usage_page": 0}]
    # Stand in for whichever backend read_scale picks.
    sys.modules["hidraw"] = stub
    sys.modules["hid"] = stub

    import read_scale

    return read_scale.main([])


if __name__ == "__main__":
    sys.exit(main())
