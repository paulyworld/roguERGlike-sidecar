# Send a single JSON command to a running sidecar's WS server.
# Useful for manual smoke tests of commands without client UI yet
# (pause/resume, set_simulation, annotate, ...).
#
# Usage:
#   ./scripts/send-cmd.ps1 '{"type":"pause","reason":"smoke","target_watts":50}'
#   ./scripts/send-cmd.ps1 '{"type":"resume"}'
#   ./scripts/send-cmd.ps1 '{"type":"set_simulation","grade_percent":6.0}'
#   ./scripts/send-cmd.ps1 '{"type":"set_target_power","watts":150}'
#
# The sidecar's typed ack envelope (target_power_set, paused, resumed,
# simulation_set, rider_annotation) will appear in the sidecar's stdout
# and in the JSONL if -Record is active. Tail the sidecar window or the
# recording to confirm receipt.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Command,

    [string]$Url = "ws://localhost:8421"
)

$ErrorActionPreference = "Stop"

# Validate JSON shape before sending so a typo doesn't waste a sidecar
# round-trip (also prevents the obvious WS-frame-isn't-json failure mode).
try {
    $null = $Command | ConvertFrom-Json
} catch {
    Write-Host "send-cmd: not valid JSON: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

# Resolve python the same way run-live-test.ps1 does — prefer the
# installed entry point, fall back to `python -m`.
$pythonArgs = @("-c", @"
import asyncio
import sys
import websockets

async def main():
    async with websockets.connect("$Url") as ws:
        await ws.send(r'''$Command''')
        print(f"sent → $Url: $Command")

asyncio.run(main())
"@)

& python $pythonArgs
