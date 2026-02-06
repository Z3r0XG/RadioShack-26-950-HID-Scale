# RadioShack 26-950 USB Scale Driver

Cross-platform utility for the RadioShack 26-950 USB scale. Reads the raw HID stream directly, bypassing obsolete legacy drivers.

## Installation

```bash
git clone https://github.com/Z3r0XG/RadioShack-26-950-HID-Scale.git
cd RadioShack-26-950-HID-Scale
pip install hidapi
python3 read_scale.py
```

## Requirements

**Hardware:**
- RadioShack 26-950 Scale (VID: `0x2233`, PID: `0x6323`)

**Software:**
- Python 3.7+
- `hidapi` package (`pip install hidapi`)

**System libraries** (if hidapi pip install fails):
- Ubuntu/Debian: `sudo apt install libhidapi-dev`
- Fedora/RHEL: `sudo dnf install hidapi-devel`
- macOS: `brew install hidapi`

**Linux permissions:** Non-root USB access may require udev rules or `sudo`

## Usage

Once running, use these keys to control the scale:
- **T** — Tare (zero out current weight)
- **R** — Reset (return to absolute hardware zero)
- **M** — Toggle units (switch between lb/oz and metric g/kg)
- **ESC** — Exit

## Technical Details

The RadioShack 26-950 transmits weight as a 16-bit Big-Endian integer in bytes 6–7 of the HID report.

- **Internal Resolution:** ~0.013 oz per raw unit
- **Calibration Factor:** 0.01286 (raw units to ounces)
- **Metric Conversion:** 1 oz = 28.3495 g

## License

GPL-3.0
