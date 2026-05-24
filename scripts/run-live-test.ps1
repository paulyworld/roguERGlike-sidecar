# Launch the sidecar in live mode against the KICKR + Whoop with trainer
# control enabled. Tweak the parameters at the top, then run from anywhere:
#
#   ./scripts/run-live-test.ps1
#   ./scripts/run-live-test.ps1 -RiderFtp 275
#   ./scripts/run-live-test.ps1 -Bike "KICKR" -Hr "TICKR"
#   ./scripts/run-live-test.ps1 -Mock          # off-bike dev path
#   ./scripts/run-live-test.ps1 -Record        # capture JSONL to docs/recordings/
#   ./scripts/run-live-test.ps1 -RecordPath rides/test.jsonl
#
# Why a script and not a pasted one-liner: PowerShell backtick line
# continuation is whitespace-sensitive and breaks on terminal/clipboard
# quirks. Splatting an array in a .ps1 sidesteps all of that.

[CmdletBinding()]
param(
    [string]$Bike = "KICKR",
    [string]$Hr = "mudrat",
    [int]$RiderFtp = 250,
    [int]$MaxTargetPower = 800,
    [int]$ScanTimeoutS = 60,
    [int]$DisconnectBailoutS = 10,
    [switch]$Mock,
    [switch]$NoTrainerControl,
    [switch]$PreferBikeHr,
    # -Record               → auto-timestamped path under docs/recordings/
    # -RecordPath <path>    → explicit path (overrides auto)
    # (omit both)           → no recording
    [switch]$Record,
    [string]$RecordPath
)

$ErrorActionPreference = "Stop"

# Resolve sidecar entrypoint. Prefer the console script if it's on PATH;
# otherwise fall back to `python -m` so this works in a fresh venv before
# `pip install -e .` registers the script.
$entry = Get-Command roguerglike-sidecar -ErrorAction SilentlyContinue
if ($null -ne $entry) {
    $exe = $entry.Source
    $exeArgs = @()
} else {
    $exe = "python"
    $exeArgs = @("-m", "roguerglike_sidecar")
}

$cliArgs = @()
if ($Mock) {
    $cliArgs += @("--mode", "mock")
} else {
    $cliArgs += @("--mode", "live")
    $cliArgs += @("--device-bike", $Bike)
    if ($Hr) { $cliArgs += @("--device-hr", $Hr) }
    $cliArgs += @("--scan-timeout-s", "$ScanTimeoutS")
}

if (-not $NoTrainerControl) {
    $cliArgs += @("--allow-trainer-control")
    $cliArgs += @("--rider-ftp", "$RiderFtp")
    $cliArgs += @("--max-target-power", "$MaxTargetPower")
    $cliArgs += @("--disconnect-bailout-s", "$DisconnectBailoutS")
}

if ($PreferBikeHr) { $cliArgs += @("--prefer-bike-hr") }

if ($Record -or $RecordPath) {
    if (-not $RecordPath) {
        $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
        $RecordPath = Join-Path -Path "docs" -ChildPath "recordings/$stamp.jsonl"
    }
    $cliArgs += @("--record", $RecordPath)
    Write-Host "Recording to $RecordPath" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Launching sidecar:" -ForegroundColor Cyan
Write-Host "  $exe $($exeArgs -join ' ') $($cliArgs -join ' ')" -ForegroundColor DarkGray
Write-Host ""
Write-Host "Success markers to watch for (in order):" -ForegroundColor Cyan
Write-Host "  1. matched FTMS Bike -> ... / matched HR Sensor -> ..." -ForegroundColor DarkGray
Write-Host "  2. device_connected kind=bike_trainer / kind=hr_monitor" -ForegroundColor DarkGray
Write-Host "  3. device_capabilities supports_target_power=true" -ForegroundColor DarkGray
Write-Host "  4. '<device>: claimed control - set_target_power now active'" -ForegroundColor DarkGray
Write-Host "  5. target_power_set accepted=true (once the engine writes)" -ForegroundColor DarkGray
Write-Host ""

& $exe @exeArgs @cliArgs
