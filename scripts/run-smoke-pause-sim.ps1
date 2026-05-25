# Hands-free smoke runner for Pattern B structured pause + SIM mode.
# Connects to a running sidecar (start it with run-live-test.ps1 first),
# auto-paces a ~2-minute sequence of WS commands, prints sidecar acks
# inline. The rider just pedals.
#
# Usage (in a second terminal while the sidecar is running):
#   ./scripts/run-smoke-pause-sim.ps1
#
# What it tests:
#   1. ERG baseline at 150W
#   2. Structured pause to 50W
#   3. set_target_power while paused (expect 'deferred-paused' rejection)
#   4. Resume → ramp to the deferred 200W
#   5. SIM mode at +5%, +9%, -3% grade
#   6. Switch back to ERG at 150W
#
# Pair with run-live-test.ps1 -Record so the JSONL captures the whole
# sequence for post-ride analysis.

[CmdletBinding()]
param(
    [string]$Url = "ws://localhost:8421"
)

$ErrorActionPreference = "Stop"

$pyScript = Join-Path $PSScriptRoot "_smoke_pause_sim.py"
& python $pyScript $Url
