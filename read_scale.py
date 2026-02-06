#!/usr/bin/env python3
"""
RadioShack 26-950 USB Scale Reader for Linux/macOS

A Python implementation of the scale reader that works on Unix-like systems.
Uses hidapi for cross-platform HID device communication.

Usage:
    python3 read_scale.py

Dependencies:
    pip install hidapi

Controls:
    T - Tare (zero current weight)
    R - Reset (return to absolute hardware zero)
    M - Toggle units (metric ↔ imperial)
    ESC or Ctrl-C - Exit
"""

import sys
import struct
import logging
import threading
import time
import signal
import select
from typing import Optional, Tuple

try:
    import hid
except ImportError:
    print("Error: hidapi not found. Install with: pip install hidapi")
    sys.exit(1)

# ============================================================================
# Configuration Constants - All magic numbers properly documented
# ============================================================================

# HID Device identifiers for RadioShack 26-950 USB Scale
DEVICE_VID = 0x2233
DEVICE_PID = 0x6323

# Buffer indices where the 16-bit Big-Endian weight value is stored
BUFFER_WEIGHT_INDEX_HIGH = 6   # High byte of weight value
BUFFER_WEIGHT_INDEX_LOW = 7    # Low byte of weight value

# Weight calculation calibration (raw units to ounces)
# Based on device specs: ~0.013 oz per raw unit
WEIGHT_CALIBRATION_MULTIPLIER = 0.01286

# Unit conversion factors
GRAMS_PER_OUNCE = 28.3495
OUNCES_PER_POUND = 16
GRAMS_PER_KILOGRAM = 1000

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s"
)
logger = logging.getLogger(__name__)

# ============================================================================
# Helper Functions
# ============================================================================


def get_raw_weight(buffer: bytes) -> int:
    """
    Extract raw weight value from HID buffer (16-bit Big-Endian integer).

    The RadioShack 26-950 encodes weight at bytes 6-7 of the HID report
    in Big-Endian format (high byte first, then low byte).

    Args:
        buffer: The HID input report buffer

    Returns:
        The 16-bit weight value
    """
    if len(buffer) < 8:
        raise ValueError(f"Buffer too small: {len(buffer)} bytes (need at least 8)")
    return (buffer[BUFFER_WEIGHT_INDEX_HIGH] * 256) + buffer[BUFFER_WEIGHT_INDEX_LOW]


def get_calibrated_weight(raw_value: int, tare_offset: int) -> float:
    """
    Calculate calibrated weight in ounces from raw value.

    Args:
        raw_value: The raw 16-bit device reading
        tare_offset: The offset to subtract for tare functionality

    Returns:
        Weight in ounces (can be negative)
    """
    delta = raw_value - tare_offset
    return delta * WEIGHT_CALIBRATION_MULTIPLIER


def format_weight_display(weight_oz: float, is_metric: bool) -> str:
    """
    Format weight for display in either metric (g/kg) or imperial (oz/lb) units.

    Handles negative weights, proper singular/plural conventions, and
    appropriate precision based on magnitude.

    Args:
        weight_oz: Weight in ounces (can be negative)
        is_metric: True for metric units (g/kg), False for imperial (oz/lb)

    Returns:
        Formatted weight string ready for display
    """
    abs_weight = abs(weight_oz)
    prefix = "-" if weight_oz < -0.01 else ""

    if is_metric:
        # Convert ounces to grams
        grams = weight_oz * GRAMS_PER_OUNCE

        if abs(grams) >= GRAMS_PER_KILOGRAM:
            # Use kilograms for large weights
            kg = abs(grams) / GRAMS_PER_KILOGRAM
            return f"{prefix}{kg:.3f} kg"
        else:
            # Use grams for smaller weights
            return f"{prefix}{abs(grams):.1f} g"
    else:
        # Imperial: pounds and ounces
        if abs_weight >= OUNCES_PER_POUND:
            lbs = int(abs_weight // OUNCES_PER_POUND)
            rem_oz = abs_weight % OUNCES_PER_POUND
            return f"{prefix}{lbs} lb {rem_oz:.2f} oz"
        else:
            # Just ounces for smaller weights
            return f"{prefix}{abs_weight:.2f} oz"


def connect_scale(vendor_id: int = DEVICE_VID, product_id: int = DEVICE_PID) -> Optional[hid.device]:
    """
    Connect to the RadioShack 26-950 HID device and return the opened stream.

    Args:
        vendor_id: USB Vendor ID (default: RadioShack 26-950)
        product_id: USB Product ID (default: RadioShack 26-950)

    Returns:
        An opened hid.device object, or None if connection failed
    """
    try:
        # First, enumerate to find the device
        devices = hid.enumerate(vendor_id, product_id)
        if not devices:
            logger.debug(f"No devices found with VID=0x{vendor_id:04X} PID=0x{product_id:04X}")
            return None
        
        # Open the first device found
        device = hid.device()
        device.open_path(devices[0]['path'])
        device_info = device.get_manufacturer_string()
        logger.info(f"Successfully opened HID device: {device_info}")
        return device
    except OSError as e:
        logger.debug(f"OSError opening device: {e}")
        return None
    except Exception as e:
        logger.debug(f"Exception opening device: {e}")
        return None


def show_help_message():
    """Display startup instructions."""
    print("RadioShack 26-950 | T: Tare | R: Reset | M: Units | ESC: Exit")


def show_error_message():
    """Display error and troubleshooting information."""
    print(
        """Device not found or could not be opened.

Troubleshooting steps:
  - Make sure the RadioShack 26-950 scale is connected and powered on.
  - Verify the USB VID/PID (expected VID=0x2233, PID=0x6323).
  - Check device permissions: you may need to run with sudo or configure udev rules.
  - Ensure no other application is currently using the device.
  - On Linux, verify the device appears in: lsusb | grep 2233
"""
    )


# ============================================================================
# Main Script Logic
# ============================================================================


def keyboard_listener(state: dict, stop_event: threading.Event, action_lock: threading.Lock):
    """Background thread that listens for keyboard input with timeout."""
    import termios
    import tty
    
    last_action_time = 0
    
    # Save original terminal settings
    try:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        tty.setraw(fd)
    except:
        fd = None
    
    try:
        while not stop_event.is_set():
            # Use select with 100ms timeout for responsive exit
            if select.select([sys.stdin], [], [], 0.1)[0]:
                try:
                    key = sys.stdin.read(1)
                    if not key:
                        break
                    
                    key_upper = key.upper()
                    
                    # Debounce: ignore input within 200ms of last action
                    current_time = time.time()
                    if current_time - last_action_time < 0.2:
                        continue
                    
                    # Thread-safe state update
                    with action_lock:
                        if key_upper == "T":
                            state["action"] = "tare"
                            last_action_time = current_time
                        elif key_upper == "R":
                            state["action"] = "reset"
                            last_action_time = current_time
                        elif key_upper == "M":
                            state["action"] = "toggle_units"
                            last_action_time = current_time
                        elif key == "\x1b":  # ESC key
                            state["action"] = "exit_esc"
                            break
                        elif key == "\x03":  # Ctrl-C
                            state["action"] = "exit_ctrl_c"
                            break
                except Exception as e:
                    logger.debug(f"Key read error: {e}")
    finally:
        # Restore terminal settings
        if fd is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except:
                pass


def main():
    """Main event loop for reading and displaying weight data."""
    stream = connect_scale()

    if stream is None:
        show_error_message()
        return

    def signal_handler(signum, frame):
        """Handle Ctrl-C gracefully."""
        nonlocal should_exit
        should_exit = True

    should_exit = False
    signal.signal(signal.SIGINT, signal_handler)

    try:
        # Initialize and read absolute zero calibration point
        data = stream.read(64)  # Standard HID buffer size
        if not data:
            logger.error("Failed to read initial data from device")
            return

        absolute_zero = get_raw_weight(bytes(data))
        current_offset = absolute_zero
        is_metric = False

        show_help_message()

        # Start keyboard listener thread with thread-safe action state
        state = {"action": None}
        action_lock = threading.Lock()
        stop_event = threading.Event()
        kb_thread = threading.Thread(
            target=keyboard_listener, args=(state, stop_event, action_lock), daemon=True
        )
        kb_thread.start()

        current_raw = absolute_zero

        # Main event loop
        try:
            stream.set_nonblocking(1)
            while not should_exit:
                # Check for pending keyboard action (thread-safe)
                with action_lock:
                    action = state.get("action")
                    if action:
                        state["action"] = None  # Clear action atomically
                
                if action == "tare":
                    current_offset = current_raw
                elif action == "reset":
                    current_offset = absolute_zero
                elif action == "toggle_units":
                    is_metric = not is_metric
                elif action in ("exit_esc", "exit_ctrl_c"):
                    break

                # Read device data
                try:
                    data = stream.read(64)  # Standard HID buffer size
                    if data:
                        current_raw = get_raw_weight(bytes(data))
                        weight_oz = get_calibrated_weight(current_raw, current_offset)
                        delta = current_raw - current_offset
                        display = format_weight_display(weight_oz, is_metric)

                        sys.stdout.write(f"\rWeight: {display} (Raw: {delta})      ")
                        sys.stdout.flush()
                except Exception as e:
                    logger.debug(f"Read error: {e}")

                # Small sleep to prevent busy waiting
                time.sleep(0.01)

        finally:
            stop_event.set()
            kb_thread.join(timeout=1.0)

    finally:
        stream.close()
        logger.info("HID stream closed")
        print()  # Final newline for clean terminal


if __name__ == "__main__":
    main()
