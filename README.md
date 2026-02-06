# RadioShack 26-950 USB Scale Driver

PowerShell utility for the RadioShack 26-950 USB scale. This script reads the raw HID stream directly via HidSharp, bypassing obsolete legacy drivers.

## Quick Start

Run these commands in a PowerShell terminal to clone, fetch dependencies, and start weighing:

```powershell
# 1. Clone the repository
git clone https://github.com/Z3r0XG/RadioShack-26-950-HID-Scale.git
cd RadioShack-26-950-HID-Scale

# 2. Download and extract HidSharp.dll
$url = "https://www.nuget.org/api/v2/package/HidSharp/2.1.0"
Invoke-WebRequest -Uri $url -OutFile "hidsharp.zip"
Expand-Archive -Path "hidsharp.zip" -DestinationPath "temp_hid" -Force
Move-Item "temp_hid\lib\net45\HidSharp.dll" ".\HidSharp.dll"
Remove-Item "hidsharp.zip", "temp_hid" -Recurse

# 3. Run the script
.\ReadScale.ps1
```

## Requirements
* **OS:** Windows 10/11
* **Hardware:** RadioShack 26-950 Scale (VID: `0x2233`, PID: `0x6323`)
* **Dependency:** `HidSharp.dll` (Fetched via the setup commands above)

## Controls
Once the script is running, use these keys:
* **T**: **Tare** (Zero out current weight)
* **R**: **Reset** (Return to absolute hardware zero)
* **M**: **Toggle Units** (Switch between lb/oz and kg/g)
* **ESC**: **Exit** (Closes HID stream and exits)

## Technical Details
The scale sends data as a HID report. The weight is stored as a 16-bit Big-Endian integer in bytes 7 and 8 of the input buffer.

* **Internal Resolution:** ~0.013 oz
* **Calibration Multiplier:** 0.01286
* **Conversion Factor:** 1 oz = 28.3495 g

## License
GPL-3.0
