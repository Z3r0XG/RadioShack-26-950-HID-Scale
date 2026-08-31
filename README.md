# RadioShack 26-950 USB Scale Driver

Reads the RadioShack 26-950 USB postal scale directly, so the discontinued
Windows-only software that shipped with it is not needed.

Tested on Linux, macOS and Windows, with Python 3.9 through 3.13.

## Install

Requires Python 3.9 or newer.

```bash
git clone https://github.com/Z3r0XG/RadioShack-26-950-HID-Scale.git
cd RadioShack-26-950-HID-Scale
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 read_scale.py
```

Windows uses `.venv\Scripts\activate` in place of the `source` line. Activate
the environment again in each new shell before running the scale.

Most current systems refuse to install packages outside a virtual environment,
so those steps are required rather than optional. If `python3 -m venv` is
missing on Debian or Ubuntu, install it with `sudo apt install python3-venv`.

`pip install hidapi` builds from source on some systems. If it fails, the
system library may be needed first:

| Platform | Command |
| --- | --- |
| Debian/Ubuntu | `sudo apt install libhidapi-dev` |
| Fedora/RHEL | `sudo dnf install hidapi-devel` |
| macOS | `brew install hidapi` |
| Windows | No system package needed |

### Linux permissions

Linux gives the scale to root only, so either run it with `sudo` or install the
bundled rule once and run it as yourself:

```bash
sudo cp 99-radioshack-scale.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Replug the scale afterwards if it is already connected. macOS and Windows need
no equivalent.

## Usage

`python3 read_scale.py --help` lists every option.

### Live readout

```console
$ python3 read_scale.py
RadioShack 26-950 | T: Tare | R: Reset | M: Units | ESC: Exit
Weight: 1 lb 4.25 oz
```

| Key | Action |
| --- | --- |
| `T` | Tare — treat the current weight as zero |
| `R` | Reset the tare back to the zero measured at startup |
| `M` | Toggle imperial (lb/oz) and metric (g/kg) |
| `ESC` or `Ctrl-C` | Exit |

`--metric` starts in g/kg instead of lb/oz.

### If it will not connect

```bash
python3 read_scale.py --list      # show what the computer can see
python3 read_scale.py -vv         # show the underlying error
```

## Hardware

- RadioShack 26-950 USB scale
- Capacity 10 lb / 5 kg

## Accuracy

Weights are calibrated against a known reference mass, measured on one scale.
Another unit may differ a little. If yours reads consistently high or low,
`--calibration` adjusts the conversion — and an issue saying what you weighed
and what it showed is welcome, since that is how the default improves.

## Problems and requests

Please [open an issue](https://github.com/Z3r0XG/RadioShack-26-950-HID-Scale/issues).
Readings that look wrong, a scale that will not connect, or a rebadged unit
that behaves differently are all worth reporting; include the output of
`python3 read_scale.py --list` and `python3 read_scale.py -vv`.

## License

GPL-3.0
