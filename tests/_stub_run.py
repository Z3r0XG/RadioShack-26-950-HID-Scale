"""
Runs read_scale's CLI against a stub device, for tests that need a real
process. Arguments after the script name are passed through to main().
Not collected by pytest; spawned by test_cli.py.
"""

import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ZERO_RAW = 56527
ZERO_SAMPLES = 20


class _StubDevice:
    """
    Steady while the zero is measured, then alternating.

    The readout only writes when the displayed weight changes, so a stub
    reporting one value forever produces a single line and never touches
    stdout again.
    """

    def __init__(self):
        self._reads = 0

    def open_path(self, path):
        pass

    def read(self, size, timeout_ms=0):
        time.sleep(0.02)
        self._reads += 1
        raw = ZERO_RAW
        if self._reads > ZERO_SAMPLES and self._reads % 2:
            raw += 40
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
    sys.modules["hidraw"] = stub
    sys.modules["hid"] = stub

    import read_scale

    return read_scale.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
