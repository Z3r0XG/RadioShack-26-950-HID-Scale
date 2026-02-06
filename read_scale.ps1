# ============================================================================
# Configuration Constants - All magic numbers properly documented
# ============================================================================

# HID Device identifiers for RadioShack 26-950 USB Scale
$DEVICE_VID = 0x2233
$DEVICE_PID = 0x6323

# Buffer indices where the 16-bit Big-Endian weight value is stored
$BUFFER_WEIGHT_INDEX_HIGH = 6  # High byte of weight value
$BUFFER_WEIGHT_INDEX_LOW = 7   # Low byte of weight value

# Weight calculation calibration (raw units to ounces)
# Based on device specs: ~0.013 oz per raw unit
$WEIGHT_CALIBRATION_MULTIPLIER = 0.01286

# Unit conversion factors
$GRAMS_PER_OUNCE = 28.3495
$OUNCES_PER_POUND = 16
$GRAMS_PER_KILOGRAM = 1000

# ============================================================================
# Helper Functions
# ============================================================================

<#
.SYNOPSIS
    Extracts raw weight value from HID buffer (16-bit Big-Endian integer)
.DESCRIPTION
    The RadioShack 26-950 encodes weight at bytes 6-7 of the HID report
    in Big-Endian format (high byte first, then low byte).
.PARAMETER Buffer
    The HID input report buffer
.OUTPUTS
    [int] The 16-bit weight value
#>
function Get-RawWeight {
    param([byte[]]$Buffer)
    return ($Buffer[$BUFFER_WEIGHT_INDEX_HIGH] * 256) + $Buffer[$BUFFER_WEIGHT_INDEX_LOW]
}

<#
.SYNOPSIS
    Calculates calibrated weight in ounces from raw value
.PARAMETER RawValue
    The raw 16-bit device reading
.PARAMETER TareOffset
    The offset to subtract for tare functionality
.OUTPUTS
    [double] Weight in ounces
#>
function Get-CalibratedWeight {
    param(
        [int]$RawValue,
        [int]$TareOffset
    )
    $delta = $RawValue - $TareOffset
    return $delta * $WEIGHT_CALIBRATION_MULTIPLIER
}

<#
.SYNOPSIS
    Formats weight for display in either metric (g/kg) or imperial (oz/lb) units
.DESCRIPTION
    Handles negative weights, proper singular/plural conventions, and
    appropriate precision based on magnitude.
.PARAMETER WeightOz
    Weight in ounces (can be negative)
.PARAMETER IsMetric
    $true for metric units (g/kg), $false for imperial (oz/lb)
.OUTPUTS
    [string] Formatted weight string ready for display
#>
function Format-WeightDisplay {
    param(
        [double]$WeightOz,
        [bool]$IsMetric
    )
    
    $absWeight = [Math]::Abs($WeightOz)
    $prefix = if ($WeightOz -lt -0.01) { "-" } else { "" }

    if ($IsMetric) {
        # Convert ounces to grams
        $grams = $WeightOz * $GRAMS_PER_OUNCE
        
        if ([Math]::Abs($grams) -ge $GRAMS_PER_KILOGRAM) {
            # Use kilograms for large weights
            $kg = [Math]::Abs($grams) / $GRAMS_PER_KILOGRAM
            return "{0}{1:N3} kg" -f $prefix, $kg
        } else {
            # Use grams for smaller weights
            return "{0}{1:N1} g " -f $prefix, [Math]::Abs($grams)
        }
    } else {
        # Imperial: pounds and ounces
        if ($absWeight -ge $OUNCES_PER_POUND) {
            $lbs = [Math]::Floor($absWeight / $OUNCES_PER_POUND)
            $remOz = $absWeight % $OUNCES_PER_POUND
            return "{0}{1} lb {2:N2} oz" -f $prefix, $lbs, $remOz
        } else {
            # Just ounces for smaller weights
            return "{0}{1:N2} oz" -f $prefix, $absWeight
        }
    }
}

<#
.SYNOPSIS
    Connects to the RadioShack 26-950 HID device and initializes the stream
.OUTPUTS
    [object] The opened stream object, or $null if connection failed
#>
function Connect-Scale {
    param(
        [uint32]$VendorId = $DEVICE_VID,
        [uint32]$ProductId = $DEVICE_PID
    )
    
    $device = [HidSharp.DeviceList]::Local.GetHidDeviceOrNull($VendorId, $ProductId)
    
    if ($null -eq $device) {
        Write-Verbose "Device with VID=0x$('{0:X4}' -f $VendorId) PID=0x$('{0:X4}' -f $ProductId) not found"
        return $null
    }
    
    $stream = $null
    if ($device.TryOpen([ref]$stream)) {
        Write-Verbose "Successfully opened HID device: $($device.GetFriendlyName())"
        return $stream
    }
    
    Write-Verbose "Failed to open device (may be in use by another process)"
    return $null
}

# ============================================================================
# Main Script Logic
# ============================================================================

# Load the HID library
$dllPath = Join-Path $PSScriptRoot "HidSharp.dll"
if (-not (Test-Path $dllPath)) { 
    throw "HidSharp.dll missing. Download from repo root." 
}
Add-Type -Path $dllPath

# Attempt to connect to the scale
$stream = Connect-Scale

if ($null -ne $stream) {
    try {
        # Initialize and read absolute zero calibration point
        $buffer = New-Object byte[] $stream.BaseStream.Length
        $null = $stream.Read($buffer, 0, $buffer.Length)
        $absoluteZero = Get-RawWeight $buffer
        $currentOffset = $absoluteZero
        $isMetric = $false

        Write-Host "RadioShack 26-950 | T: Tare | R: Reset | M: Units | ESC: Exit"

        # Main event loop
        while ($true) {
            # Check for keyboard input
            if ([Console]::KeyAvailable) {
                $key = [Console]::ReadKey($true).Key
                if ($key -eq 'T') { 
                    $currentOffset = $currentRaw 
                    Write-Verbose "Tare set to raw value: $currentRaw"
                }
                if ($key -eq 'R') { 
                    $currentOffset = $absoluteZero 
                    Write-Verbose "Reset to absolute zero"
                }
                if ($key -eq 'M') { 
                    $isMetric = -not $isMetric 
                    $units = if ($isMetric) { "Metric" } else { "Imperial" }
                    Write-Verbose "Switched to $units units"
                }
                if ($key -eq 'Escape') { 
                    break 
                }
            }

            # Read weight data from device
            if ($stream.Read($buffer, 0, $buffer.Length) -gt 0) {
                $currentRaw = Get-RawWeight $buffer
                $weightOz = Get-CalibratedWeight -RawValue $currentRaw -TareOffset $currentOffset
                $delta = $currentRaw - $currentOffset
                $display = Format-WeightDisplay -WeightOz $weightOz -IsMetric $isMetric
                
                Write-Host ("`rWeight: $display (Raw: $delta)      ") -NoNewline
            }
        }
    } finally { 
        $stream.Close()
        Write-Verbose "HID stream closed"
    }
} else {
    Write-Host @"
Device not found or could not be opened.

Troubleshooting steps:
  - Make sure the RadioShack 26-950 scale is connected and powered on.
  - Verify the USB VID/PID (expected VID=0x2233, PID=0x6323).
  - Check that the required USB/HID drivers are installed and the device appears in Device Manager.
  - If running on Windows, try launching PowerShell as Administrator and re-running this script.
  - Ensure no other application is currently using the device.
"@
}
