$dllPath = Join-Path $PSScriptRoot "HidSharp.dll"
if (-not (Test-Path $dllPath)) { 
    throw "HidSharp.dll missing. Download from repo root." 
}

Add-Type -Path $dllPath
$device = [HidSharp.DeviceList]::Local.GetHidDeviceOrNull(0x2233, 0x6323)
$stream = $null

if ($device.TryOpen([ref]$stream)) {
    $buffer = New-Object byte[] $device.MaxInputReportLength
    $null = $stream.Read($buffer, 0, $buffer.Length)
    $absoluteZero = ($buffer[7] * 256) + $buffer[8]
    $currentOffset = $absoluteZero
    $isMetric = $false

    Write-Host "RadioShack 26-950 | T: Tare | R: Reset | M: Units | ESC: Exit"

    try {
        while ($true) {
            if ([Console]::KeyAvailable) {
                $key = [Console]::ReadKey($true).Key
                if ($key -eq 'T') { $currentOffset = $currentRaw }
                if ($key -eq 'R') { $currentOffset = $absoluteZero }
                if ($key -eq 'M') { $isMetric = -not $isMetric }
                if ($key -eq 'Escape') { break }
            }

            if ($stream.Read($buffer, 0, $buffer.Length) -gt 0) {
                $currentRaw = ($buffer[7] * 256) + $buffer[8]
                $delta = $currentRaw - $currentOffset
                $totalOz = $delta * 0.01286
                $absTotalOz = [Math]::Abs($totalOz)
                $prefix = if ($totalOz -lt -0.01) { "-" } else { "" }

                if ($isMetric) {
                    $g = $totalOz * 28.3495
                    if ([Math]::Abs($g) -ge 1000) {
                        $display = "{0}{1:N3} kg" -f $prefix, ([Math]::Abs($g) / 1000)
                    } else {
                        $display = "{0}{1:N1} g " -f $prefix, [Math]::Abs($g)
                    }
                } else {
                    if ($absTotalOz -ge 16) {
                        $lbs = [Math]::Floor($absTotalOz / 16)
                        $remOz = $absTotalOz % 16
                        $display = "{0}{1} lb {2:N2} oz" -f $prefix, $lbs, $remOz
                    } else {
                        $display = "{0}{1:N2} oz" -f $prefix, $absTotalOz
                    }
                }
                Write-Host ("`rWeight: $display (Raw: $delta)      ") -NoNewline
            }
        }
    } finally { $stream.Close() }
} else {
    Write-Host "Device not found."
}
