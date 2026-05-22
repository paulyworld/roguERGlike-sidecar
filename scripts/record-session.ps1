# Connect to the running sidecar's WebSocket and record every event to a
# timestamped JSONL file under docs/recordings/. Pairs with run-live-test.ps1
# — start the sidecar in one window, this recorder in another.
#
#   ./scripts/record-session.ps1
#   ./scripts/record-session.ps1 -OutputName 2026-05-22-hiit-test
#   ./scripts/record-session.ps1 -Url ws://localhost:8421
#
# Stop with Ctrl+C. File is flushed on every write so a crash mid-ride still
# leaves a usable recording. **Recordings contain HR samples and other
# personal telemetry** — docs/recordings/ is gitignored, keep it that way.

[CmdletBinding()]
param(
    [string]$Url = "ws://localhost:8421",
    [string]$OutputName = "",
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$recorder = Join-Path $repoRoot "scripts\record_session.py"

$pyArgs = @($recorder, "--url", $Url)

if ($OutputPath) {
    $pyArgs += @("--output", $OutputPath)
} elseif ($OutputName) {
    $recordingsDir = Join-Path $repoRoot "docs\recordings"
    if (-not (Test-Path $recordingsDir)) {
        New-Item -ItemType Directory -Path $recordingsDir | Out-Null
    }
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $file = Join-Path $recordingsDir "${stamp}_${OutputName}.jsonl"
    $pyArgs += @("--output", $file)
}

Write-Host ""
Write-Host "Recording sidecar events from $Url" -ForegroundColor Cyan
Write-Host "  python $($pyArgs -join ' ')" -ForegroundColor DarkGray
Write-Host "  (Ctrl+C to stop; file is flushed continuously)" -ForegroundColor DarkGray
Write-Host ""

& python @pyArgs
