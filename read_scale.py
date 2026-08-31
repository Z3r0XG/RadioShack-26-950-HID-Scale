#!/usr/bin/env python3
"""Read weight from a RadioShack 26-950 USB scale. Run with --help for options."""

from __future__ import annotations

import argparse
import atexit
import contextlib
import errno
import importlib
import logging
import os
import signal
import statistics
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

# hidapi exposes up to two backends. On Linux it builds both: hidraw drives
# /dev/hidraw*, and hid goes through libusb, which additionally needs
# /dev/bus/usb. macOS and Windows ship hid alone. Which one can reach the
# device depends on how the machine grants access, so both are tried in turn
# rather than one being assumed.
BACKEND_NAMES = ("hidraw", "hid")

__version__ = "1.0.0"

# ============================================================================
# Device and protocol constants
# ============================================================================

# USB identifiers for the RadioShack 26-950. The same hardware was rebadged
# by other brands; --vid/--pid let you point the reader at those.
DEVICE_VID = 0x2233
DEVICE_PID = 0x6323

# The report is 8 vendor-defined bytes (HID usage page 0xFF00, no report ID):
# a fixed 6-byte header, then the weight as an unsigned 16-bit big-endian
# value. Overridable because the layout is device-specific, not standardised.
DEFAULT_WEIGHT_OFFSET = 6
DEFAULT_ENDIANNESS = "big"

# Raw counts to ounces. Device-specific; --calibration overrides it.
DEFAULT_CALIBRATION = 0.012764

# Rated capacity of the 26-950: 10 lb / 5 kg.
DEFAULT_CAPACITY_OZ = 160.0

# Readings beyond capacity by more than this factor indicate an over-range
# condition or a decode error, not a real weight.
OVER_RANGE_MARGIN = 1.10

# Unit conversions.
GRAMS_PER_OUNCE = 28.3495
OUNCES_PER_POUND = 16
GRAMS_PER_KILOGRAM = 1000

# ============================================================================
# Timing and reliability constants
# ============================================================================

# Each HID read blocks in the kernel for at most this long, so the loop waits
# on the device instead of spinning. Also bounds keypress latency.
READ_TIMEOUT_MS = 100

# How long to wait for the device's first report before giving up.
FIRST_REPORT_TIMEOUT_S = 5.0

# Consecutive read errors tolerated before concluding the scale is gone.
MAX_CONSECUTIVE_READ_FAILURES = 25

# How long the device may go without producing a decodable report before it
# counts as gone. Empty reads are ordinary -- the scale reports at about 3 Hz
# while reads time out ten times a second -- so silence is measured in time
# rather than in reads.
SILENCE_TIMEOUT_S = 5.0

# Zero calibration: how many samples to average at startup, and the spread
# (in raw counts) below which the platter is considered settled.
ZERO_SAMPLE_COUNT = 8
STABILITY_TOLERANCE_COUNTS = 2

# Ignore a repeat of the same T/R/M key within this window; a different key
# acts immediately.
KEY_DEBOUNCE_S = 0.2

# How long to wait for the rest of a terminal escape sequence before treating
# an ESC as the quit key on its own.
ESCAPE_SEQUENCE_TIMEOUT_S = 0.05

# ANSI erase-to-end-of-line; avoids leaving fragments of a longer previous
# reading on screen when the line shrinks.
CLEAR_TO_EOL = "\x1b[K"

logger = logging.getLogger("read_scale")

# Exit codes.
EXIT_OK = 0
EXIT_DEVICE_ERROR = 1
EXIT_INTERRUPTED = 130


class ScaleError(Exception):
    """Base class for scale failures that should exit with a message."""


class ScaleConnectionError(ScaleError):
    """The device could not be found or opened."""


class DependencyError(ScaleError):
    """The hidapi binding is missing or unusable. Not a hardware problem."""


class ShortReportError(ScaleError):
    """An input report was too short to contain the weight field."""


# ============================================================================
# Configuration
# ============================================================================


@dataclass(frozen=True)
class Config:
    """Runtime configuration, mostly protocol parameters worth overriding."""

    vid: int = DEVICE_VID
    pid: int = DEVICE_PID
    weight_offset: int = DEFAULT_WEIGHT_OFFSET
    endianness: str = DEFAULT_ENDIANNESS
    calibration: float = DEFAULT_CALIBRATION
    capacity_oz: float = DEFAULT_CAPACITY_OZ
    read_timeout_ms: int = READ_TIMEOUT_MS

    @property
    def max_plausible_counts(self) -> float:
        """Highest raw count that could be a real weight on this scale."""
        return (self.capacity_oz / self.calibration) * OVER_RANGE_MARGIN


# ============================================================================
# Decoding
# ============================================================================


def decode_raw_weight(
    report: Sequence[int],
    offset: int = DEFAULT_WEIGHT_OFFSET,
    endianness: str = DEFAULT_ENDIANNESS,
) -> int:
    """
    Extract the 16-bit weight field from an HID input report.

    Args:
        report: The bytes of one HID input report.
        offset: Index of the first of the two weight bytes.
        endianness: "big" (offset holds the high byte) or "little".

    Returns:
        The unsigned 16-bit raw count.

    Raises:
        ShortReportError: The report is too short to hold the field.
        ValueError: endianness is neither "big" nor "little".
    """
    needed = offset + 2
    if len(report) < needed:
        raise ShortReportError(
            f"report is {len(report)} bytes, need at least {needed} "
            f"for a weight field at offset {offset}"
        )

    first, second = report[offset], report[offset + 1]
    if endianness == "big":
        return (first << 8) | second
    if endianness == "little":
        return (second << 8) | first
    raise ValueError(f"endianness must be 'big' or 'little', got {endianness!r}")


def signed_delta(raw: int, zero_offset: int) -> int:
    """
    Difference between a raw count and the zero reference, wrap-aware.

    The counter is 16-bit and carries a per-unit zero offset large enough
    that it rolls over inside the scale's rated range, so a plain
    subtraction reports heavy loads as large negative counts. The signed
    reading is unambiguous over +-32767 counts, far beyond capacity, and is
    continuous across the rollover.
    """
    delta = (raw - zero_offset) & 0xFFFF
    if delta > 0x7FFF:
        delta -= 0x10000
    return delta


def wrap_aware_center(samples: Sequence[int]) -> tuple[int, int]:
    """
    Middle and spread of a group of raw counts, tolerant of the rollover.

    Comparing raw counts directly reports full-scale motion for readings that
    straddle the wrap, so offsets are taken from the first sample, which is a
    small number either side of the boundary.
    """
    anchor = samples[0]
    offsets = [signed_delta(sample, anchor) for sample in samples]
    center = (anchor + int(statistics.median(offsets))) & 0xFFFF
    return center, max(offsets) - min(offsets)


def calibrated_ounces(raw: int, zero_offset: int, calibration: float) -> float:
    """Convert a raw count to ounces relative to the current zero."""
    return signed_delta(raw, zero_offset) * calibration


def check_range(delta: int, config: Config) -> str | None:
    """
    Return a warning if a reading cannot be a real weight, else None.

    Takes the wrap-aware delta rather than the raw count: the raw value
    carries an arbitrary zero offset, and past the rollover it is a small
    number indistinguishable from a light load. Only the magnitude relative
    to zero can be judged against capacity.
    """
    if abs(delta) > config.max_plausible_counts:
        return (
            f"This reads as more than the scale's {config.capacity_oz / OUNCES_PER_POUND:.0f} lb "
            "capacity. Take the load off; if it happens with something light, "
            "please report it."
        )
    return None


# ============================================================================
# Formatting
# ============================================================================


def format_weight_display(weight_oz: float, is_metric: bool) -> str:
    """
    Format a weight for display, in metric (g/kg) or imperial (oz/lb).

    Rounding happens before pounds and ounces are split, so the ounces
    component is always under 16. The sign comes from the rounded value in
    the unit being displayed, so a reading that rounds to zero prints
    unsigned.

    Args:
        weight_oz: Weight in ounces; may be negative.
        is_metric: True for g/kg, False for oz/lb.

    Returns:
        The formatted weight.
    """
    if is_metric:
        grams = round(weight_oz * GRAMS_PER_OUNCE, 1)
        if grams == 0:  # collapse -0.0
            grams = 0.0
        if abs(grams) >= GRAMS_PER_KILOGRAM:
            kilograms = round(grams / GRAMS_PER_KILOGRAM, 3)
            return f"{kilograms:.3f} kg"
        return f"{grams:.1f} g"

    ounces = round(weight_oz, 2)
    if ounces == 0:  # collapse -0.0
        ounces = 0.0
    sign = "-" if ounces < 0 else ""
    magnitude = abs(ounces)

    if magnitude >= OUNCES_PER_POUND:
        pounds = int(magnitude // OUNCES_PER_POUND)
        remainder = round(magnitude - pounds * OUNCES_PER_POUND, 2)
        if remainder >= OUNCES_PER_POUND:  # guard against float drift
            pounds += 1
            remainder = 0.0
        return f"{sign}{pounds} lb {remainder:.2f} oz"
    return f"{sign}{magnitude:.2f} oz"


# ============================================================================
# Device access
# ============================================================================


def available_backends() -> list[tuple[str, Any]]:
    """The hidapi backends that import, in the order they should be tried."""
    found = []
    for name in BACKEND_NAMES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        if hasattr(module, "device"):
            found.append((name, module))
    return found


def require_hidapi() -> None:
    """Exit with installation guidance if the hidapi binding is unusable."""
    if not available_backends():
        raise DependencyError(
            "the 'hid' module is not installed.\n"
            "  Install it with: pip install hidapi\n"
            "  If that fails to build, install the system library first:\n"
            "    Debian/Ubuntu: sudo apt install libhidapi-dev\n"
            "    Fedora/RHEL:   sudo dnf install hidapi-devel\n"
            "    macOS:         brew install hidapi"
        )


def enumerate_scales(config: Config) -> list[tuple[str, dict[str, Any]]]:
    """Every matching HID interface, paired with the backend that found it."""
    require_hidapi()
    found = []
    for name, module in available_backends():
        try:
            for entry in module.enumerate(config.vid, config.pid):
                found.append((name, entry))
        except OSError as exc:
            logger.debug("%s backend could not enumerate: %s", name, exc)
    return found


def select_interface(devices: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """
    Pick the interface to read from.

    The scale exposes a single vendor-defined collection, so there is nothing
    to choose between. Rebadged variants are unverified; --list shows what a
    given unit exposes and --dump shows what it sends.
    """
    if len(devices) > 1:
        logger.debug("device exposes %d interfaces; using the first", len(devices))
    return devices[0]


def connect_scale(config: Config):
    """
    Open the scale and return the HID device handle.

    Raises:
        ScaleConnectionError: Not found, or found but not openable. The
            underlying OS error is included, since a permission failure and
            a missing device need completely different fixes.
    """
    require_hidapi()
    failures = []
    seen_device = False

    for name, module in available_backends():
        try:
            devices = list(module.enumerate(config.vid, config.pid))
        except OSError as exc:
            failures.append(f"{name}: enumeration failed: {exc}")
            continue
        if not devices:
            continue

        seen_device = True
        chosen = select_interface(devices)
        device = module.device()
        try:
            device.open_path(chosen["path"])
        except OSError as exc:
            failures.append(f"{name}: found the device but could not open it: {exc}")
            continue

        try:
            manufacturer = device.get_manufacturer_string() or "unknown manufacturer"
            product = device.get_product_string() or "unknown product"
        except (OSError, ValueError):
            manufacturer, product = "unknown manufacturer", "unknown product"
        logger.info("Connected: %s %s via %s", manufacturer, product, name)
        return device

    if not seen_device:
        if (config.vid, config.pid) == (DEVICE_VID, DEVICE_PID):
            raise ScaleConnectionError("no RadioShack 26-950 scale is connected")
        raise ScaleConnectionError(
            f"no device found with VID=0x{config.vid:04X} PID=0x{config.pid:04X}"
        )
    raise ScaleConnectionError("\n  ".join(failures))


def read_report(device, timeout_ms: int) -> list[int] | None:
    """
    Read one input report, blocking in the kernel for up to timeout_ms.

    Returns:
        The report bytes, or None if the device sent nothing in time.

    Raises:
        OSError: The device failed (typically unplugged).
    """
    data = device.read(64, timeout_ms)
    return data if data else None


def wait_for_first_report(device, config: Config, timeout_s: float) -> list[int]:
    """
    Block until the scale sends its first report.

    Raises:
        ScaleError: Nothing arrived within timeout_s.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        report = read_report(device, config.read_timeout_ms)
        if report:
            return report
    raise ScaleError(
        f"found the scale but it sent nothing in {timeout_s:g}s.\n"
        "  Try a different USB port or cable."
    )


def measure_zero(device, config: Config) -> int:
    """
    Establish the empty-platter reference by averaging several readings.

    Averaging rejects the noise and motion present in any single report.

    Returns:
        The median raw count to use as the zero reference.
    """
    samples: list[int] = []
    short_reports = 0
    deadline = time.monotonic() + FIRST_REPORT_TIMEOUT_S

    while len(samples) < ZERO_SAMPLE_COUNT and time.monotonic() < deadline:
        report = read_report(device, config.read_timeout_ms)
        if not report:
            continue
        try:
            samples.append(decode_raw_weight(report, config.weight_offset, config.endianness))
        except ShortReportError as exc:
            # One short report is noise; every report being short means the
            # configured offset does not fit this device.
            short_reports += 1
            logger.debug("short report while measuring zero: %s", exc)

    if not samples:
        if short_reports:
            raise ScaleError(
                "the scale is sending data this reader does not understand.\n"
                "  If this is a rebadged version of the 26-950, please open an\n"
                "  issue: https://github.com/Z3r0XG/RadioShack-26-950-HID-Scale/issues"
            )
        raise ScaleError("could not collect any readings to establish zero")

    zero, spread = wrap_aware_center(samples)

    if spread > STABILITY_TOLERANCE_COUNTS:
        logger.warning(
            "The scale was still moving when it started; press R to set zero "
            "again once it is steady."
        )

    logger.info("Zero reference: %d counts", zero)
    return zero


# ============================================================================
# Keyboard input
# ============================================================================


class KeyReader:
    """
    Non-blocking single-keypress reader.

    Works on Unix (termios) and Windows (msvcrt), and degrades to returning
    no keys when stdin is not a terminal, so piping into the reader does not
    break it. Used as a context manager; terminal state is also restored via
    atexit in case the process dies without unwinding.
    """

    def __init__(self) -> None:
        self._fd: int | None = None
        self._saved_settings = None
        self._termios = None
        self._msvcrt = None
        self._select = None

    def __enter__(self) -> KeyReader:
        if not sys.stdin.isatty():
            logger.debug("stdin is not a TTY; keyboard controls disabled")
            return self

        if os.name == "nt":
            try:
                import msvcrt

                self._msvcrt = msvcrt
            except ImportError as exc:  # pragma: no cover - Windows only
                logger.debug("msvcrt unavailable: %s", exc)
            return self

        try:
            import select
            import termios
            import tty
        except ImportError as exc:  # pragma: no cover - platform dependent
            logger.debug("termios/tty unavailable: %s", exc)
            return self

        try:
            fd = sys.stdin.fileno()
            saved = termios.tcgetattr(fd)
        except (OSError, ValueError) as exc:
            logger.debug("could not read terminal settings: %s", exc)
            return self

        # Record the saved state before touching the terminal, so a failure
        # part-way through setcbreak can still be undone.
        self._fd = fd
        self._saved_settings = saved
        self._termios = termios
        self._select = select
        atexit.register(self.restore)

        try:
            # cbreak rather than raw: ISIG stays enabled, so Ctrl-C still
            # raises SIGINT even if the main loop is wedged.
            tty.setcbreak(fd)
        except (OSError, ValueError) as exc:
            logger.debug("could not enter cbreak mode: %s", exc)
            self.restore()
        return self

    def __exit__(self, *exc_info) -> None:
        self.restore()

    def restore(self) -> None:
        """Restore the original terminal settings. Safe to call repeatedly."""
        if self._fd is None or self._saved_settings is None or self._termios is None:
            return
        fd, saved, termios_mod = self._fd, self._saved_settings, self._termios
        # Clear first so a failure here cannot cause an endless retry loop.
        self._fd = self._saved_settings = None
        try:
            termios_mod.tcsetattr(fd, termios_mod.TCSADRAIN, saved)
        except (OSError, ValueError) as exc:
            logger.debug("could not restore terminal settings: %s", exc)

    def get_key(self) -> str | None:
        """
        Return one pending keypress, or None if nothing is waiting.

        Cursor and function keys arrive as escape sequences beginning with
        ESC; those are consumed and discarded so they cannot be mistaken for
        the quit key.
        """
        if self._msvcrt is not None:  # pragma: no cover - Windows only
            if not self._msvcrt.kbhit():
                return None
            key = self._msvcrt.getwch()
            if key in ("\x00", "\xe0"):
                # Prefix of an extended key; drop the code byte that follows.
                if self._msvcrt.kbhit():
                    self._msvcrt.getwch()
                return None
            return key

        if self._fd is None:
            return None

        key = self._read_char()
        if key != "\x1b":
            return key
        # Bare ESC, or the start of a sequence. Anything that follows
        # immediately belongs to a sequence rather than to a second keypress.
        following = self._read_char(timeout=ESCAPE_SEQUENCE_TIMEOUT_S)
        if following is None:
            return key
        if following in ("[", "O"):
            while True:
                final = self._read_char(timeout=ESCAPE_SEQUENCE_TIMEOUT_S)
                if final is None or "@" <= final <= "~":
                    break
        return None

    def _read_char(self, timeout: float = 0) -> str | None:
        """
        Read one byte straight from the terminal, or None if none is ready.

        Bypasses sys.stdin because that wraps the descriptor in a buffer:
        select() reports the descriptor idle while characters sit unread in
        the buffer, so everything after the first key of a burst is stranded.
        """
        if self._fd is None or self._select is None:
            return None
        try:
            ready, _, _ = self._select.select([self._fd], [], [], timeout)
            if not ready:
                return None
            data = os.read(self._fd, 1)
        except (OSError, ValueError) as exc:
            logger.debug("keyboard read failed: %s", exc)
            return None
        if not data:
            return None
        return data.decode("latin-1")


# ============================================================================
# Live readout
# ============================================================================


def run_live(device, config: Config, args: argparse.Namespace) -> int:
    """Interactive readout with tare/reset/unit controls. Returns an exit code."""
    zero = measure_zero(device, config)
    tare = zero
    is_metric = args.metric
    last_raw = zero
    last_action = 0.0
    last_key = None
    failures = 0
    last_reading_at = time.monotonic()
    warned_over_range = False
    interactive = sys.stdout.isatty()
    last_line: str | None = None

    should_exit = False
    interrupted = False

    def handle_sigint(_signum, _frame):
        nonlocal should_exit, interrupted
        should_exit = True
        interrupted = True

    previous_handlers = {signal.SIGINT: signal.signal(signal.SIGINT, handle_sigint)}
    # Without these the process dies before the terminal is restored, since
    # atexit does not run for a signal nobody handles. SIGHUP is POSIX-only.
    for name in ("SIGTERM", "SIGHUP"):
        terminating = getattr(signal, name, None)
        if terminating is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            previous_handlers[terminating] = signal.signal(terminating, handle_sigint)

    if interactive:
        print("RadioShack 26-950 | T: Tare | R: Reset | M: Units | ESC: Exit")

    try:
        with KeyReader() as keys:
            while not should_exit:
                key = keys.get_key()
                if key:
                    upper = key.upper()
                    # Exit keys are never debounced: a user pressing ESC
                    # right after another key must still be able to quit.
                    if key == "\x03":
                        interrupted = True
                        break
                    if key == "\x1b":
                        break
                    # Debounce a held key without swallowing a different one
                    # pressed straight after it.
                    now = time.monotonic()
                    repeat = upper == last_key and now - last_action < KEY_DEBOUNCE_S
                    if not repeat:
                        if upper == "T":
                            tare = last_raw
                            warned_over_range = False
                        elif upper == "R":
                            tare = zero
                            warned_over_range = False
                        elif upper == "M":
                            is_metric = not is_metric
                        last_key, last_action = upper, now

                try:
                    report = read_report(device, config.read_timeout_ms)
                    failures = 0
                except OSError as exc:
                    failures += 1
                    logger.debug("read failed (%d consecutive): %s", failures, exc)
                    if failures >= MAX_CONSECUTIVE_READ_FAILURES:
                        _end_line(interactive)
                        logger.error("Lost contact with the scale: %s", exc)
                        return EXIT_DEVICE_ERROR
                    continue

                if report:
                    try:
                        last_raw = decode_raw_weight(
                            report, config.weight_offset, config.endianness
                        )
                        last_reading_at = time.monotonic()
                    except ShortReportError as exc:
                        logger.debug("skipping short report: %s", exc)

                if time.monotonic() - last_reading_at > SILENCE_TIMEOUT_S:
                    _end_line(interactive)
                    logger.error(
                        "No readable report from the scale in %.0fs; giving up.",
                        SILENCE_TIMEOUT_S,
                    )
                    return EXIT_DEVICE_ERROR

                if not report:
                    continue

                warning = check_range(signed_delta(last_raw, tare), config)
                if warning and not warned_over_range:
                    _end_line(interactive)
                    logger.warning("%s", warning)
                warned_over_range = warning is not None

                weight_oz = calibrated_ounces(last_raw, tare, config.calibration)
                last_line = write_reading(weight_oz, is_metric, interactive, last_line)

        return EXIT_INTERRUPTED if interrupted else EXIT_OK
    finally:
        for signum, handler in previous_handlers.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signum, handler)
        _end_line(interactive)


def _end_line(interactive: bool) -> None:
    """Move off the in-place readout line before any other output."""
    if interactive:
        sys.stdout.write("\n")
        sys.stdout.flush()


def write_reading(
    weight_oz: float, is_metric: bool, interactive: bool, last_line: str | None
) -> str:
    """
    Render one reading and return the line written.

    On a terminal the line is rewritten in place and erased to its end, so a
    shorter reading cannot leave part of a longer one behind. Redirected, the
    line is written only when it changes.
    """
    line = f"Weight: {format_weight_display(weight_oz, is_metric)}"
    if interactive:
        sys.stdout.write(f"\r{line}{CLEAR_TO_EOL}")
        sys.stdout.flush()
    elif line != last_line:
        print(line, flush=True)
    return line


def run_dump(device, config: Config, args: argparse.Namespace) -> int:
    """
    Print raw input reports as the device sends them.

    Each line carries the report in hex, the value the configured decode
    produces, the opposite byte order at the same offset, and the reading a
    standard HID Point-of-Sale scale report would give.
    """
    print(
        f"Raw reports, Ctrl-C to stop. Decoding at offset {config.weight_offset}, "
        f"{config.endianness}-endian, {config.calibration} oz per count.\n"
    )

    reference = None
    while True:
        try:
            report = read_report(device, config.read_timeout_ms)
        except OSError as exc:
            raise ScaleError(f"lost contact with the scale: {exc}") from exc
        if not report:
            continue

        hex_bytes = " ".join(f"{b:02X}" for b in report)
        print(f"len={len(report):2d}  {hex_bytes}", flush=True)

        try:
            raw = decode_raw_weight(report, config.weight_offset, config.endianness)
            if reference is None:
                reference = raw
            moved = signed_delta(raw, reference) * config.calibration
            print(
                f"    configured decode -> raw {raw}, {moved:+.3f} oz "
                f"({moved * GRAMS_PER_OUNCE:+.2f} g) against the first report"
            )
        except ShortReportError as exc:
            print(f"    configured decode -> {exc}")

        # Show the alternative byte order at the same offset; if the value
        # tracks weight linearly and the configured one does not, the
        # endianness is wrong.
        other = "little" if config.endianness == "big" else "big"
        try:
            alt = decode_raw_weight(report, config.weight_offset, other)
            print(f"    {other}-endian at same offset -> raw {alt}")
        except ShortReportError:
            pass

        print()
    return EXIT_OK


def run_list(config: Config) -> int:
    """List matching HID interfaces and exit."""
    print("backends: " + ", ".join(name for name, _ in available_backends()))
    devices = enumerate_scales(config)
    if not devices:
        print(f"Nothing found matching VID=0x{config.vid:04X} PID=0x{config.pid:04X}")
        return EXIT_DEVICE_ERROR
    for backend, device in devices:
        path = device.get("path", b"")
        print(
            f"path={path.decode(errors='replace') if isinstance(path, bytes) else path}\n"
            f"  manufacturer : {device.get('manufacturer_string') or '?'}\n"
            f"  product      : {device.get('product_string') or '?'}\n"
            f"  backend      : {backend}\n"
            f"  usage_page   : 0x{device.get('usage_page', 0):02X}\n"
            f"  interface    : {device.get('interface_number')}"
        )
    return EXIT_OK


# ============================================================================
# Entry point
# ============================================================================


def positive(name: str):
    """An argparse type for a value that must be greater than zero."""

    def parse(text: str) -> float:
        value = float(text)
        if value <= 0:
            raise argparse.ArgumentTypeError(f"{name} must be greater than zero")
        return value

    return parse


def non_negative_int(name: str):
    """An argparse type for a count or offset that cannot be negative."""

    def parse(text: str) -> int:
        value = int(text)
        if value < 0:
            raise argparse.ArgumentTypeError(f"{name} cannot be negative")
        return value

    return parse


def usb_id(text: str) -> int:
    """An argparse type for a 16-bit USB vendor or product id."""
    value = int(text, 0)
    if not 0 <= value <= 0xFFFF:
        raise argparse.ArgumentTypeError("USB ids are 16-bit (0x0000-0xFFFF)")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="read_scale.py",
        description="Read weight from a RadioShack 26-950 USB scale.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Controls during the live readout:\n"
            "  T  tare      R  reset tare      M  toggle units      ESC  exit\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    output = parser.add_argument_group("output")
    output.add_argument(
        "--metric",
        action="store_true",
        help="start in metric units (g/kg) instead of imperial",
    )
    output.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="show diagnostics; repeat (-vv) for debug detail",
    )

    device = parser.add_argument_group("device")
    device.add_argument("--list", action="store_true", help="list matching HID interfaces and exit")
    device.add_argument(
        "--vid",
        type=usb_id,
        default=DEVICE_VID,
        help=f"USB vendor ID (default: 0x{DEVICE_VID:04X})",
    )
    device.add_argument(
        "--pid",
        type=usb_id,
        default=DEVICE_PID,
        help=f"USB product ID (default: 0x{DEVICE_PID:04X})",
    )

    protocol = parser.add_argument_group(
        "protocol", "Adjust the decode for a variant that reports differently."
    )
    protocol.add_argument(
        "--dump",
        action="store_true",
        help="print raw HID reports and how they decode, then exit on Ctrl-C",
    )
    protocol.add_argument(
        "--weight-offset",
        type=non_negative_int("--weight-offset"),
        default=DEFAULT_WEIGHT_OFFSET,
        help=f"byte offset of the weight field (default: {DEFAULT_WEIGHT_OFFSET})",
    )
    protocol.add_argument(
        "--endian",
        choices=("big", "little"),
        default=DEFAULT_ENDIANNESS,
        help=f"byte order of the weight field (default: {DEFAULT_ENDIANNESS})",
    )
    protocol.add_argument(
        "--calibration",
        type=positive("--calibration"),
        default=DEFAULT_CALIBRATION,
        help=f"ounces per raw count (default: {DEFAULT_CALIBRATION})",
    )
    protocol.add_argument(
        "--capacity",
        type=positive("--capacity"),
        default=DEFAULT_CAPACITY_OZ / OUNCES_PER_POUND,
        help=f"rated capacity in pounds, for range checking "
        f"(default: {DEFAULT_CAPACITY_OZ / OUNCES_PER_POUND:.0f})",
    )
    return parser


def configure_logging(verbosity: int) -> None:
    """
    Send diagnostics to stderr so they never corrupt piped readings.

    Warnings and errors are always shown; -v adds info, -vv adds debug.
    """
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s", stream=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)

    config = Config(
        vid=args.vid,
        pid=args.pid,
        weight_offset=args.weight_offset,
        endianness=args.endian,
        calibration=args.calibration,
        capacity_oz=args.capacity * OUNCES_PER_POUND,
    )

    try:
        if args.list:
            return run_list(config)

        device = connect_scale(config)
        try:
            wait_for_first_report(device, config, FIRST_REPORT_TIMEOUT_S)
            if args.dump:
                return run_dump(device, config, args)
            return run_live(device, config, args)
        finally:
            device.close()
            logger.info("HID stream closed")

    except DependencyError as exc:
        # Deliberately no device troubleshooting: nothing about the scale is
        # wrong, the Python binding just is not installed.
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_DEVICE_ERROR
    except ScaleConnectionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print(TROUBLESHOOTING, file=sys.stderr)
        return EXIT_DEVICE_ERROR
    except ScaleError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_DEVICE_ERROR
    except OSError as exc:
        # A consumer closing the pipe raises BrokenPipeError on POSIX and
        # EINVAL on Windows. Anything else is a real fault and belongs to
        # the caller.
        if not isinstance(exc, BrokenPipeError) and exc.errno != errno.EINVAL:
            raise
        # Point stdout at devnull so the interpreter's own final flush cannot
        # raise again on the way out.
        with contextlib.suppress(OSError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_OK
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED


TROUBLESHOOTING = """
Things to try:
  - Reseat the USB connection, or try another port.
  - See what the computer can find:  python3 read_scale.py --list
  - Close any other program using the scale.
  - On Linux, permission denied means the udev rule is not installed:
      sudo cp 99-radioshack-scale.rules /etc/udev/rules.d/
      sudo udevadm control --reload-rules && sudo udevadm trigger
    then replug the scale. Running with sudo also works.
  - Show the underlying error:  python3 read_scale.py -vv

If none of that helps, please open an issue:
  https://github.com/Z3r0XG/RadioShack-26-950-HID-Scale/issues
"""


if __name__ == "__main__":
    sys.exit(main())
