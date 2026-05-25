"""Auto-paced smoke for the structured-pause + SIM-mode protocols.

Connects to a running sidecar (assumed up via run-live-test.ps1 and
already controlling a real KICKR), then walks through a scripted
~100-second sequence of WS commands while the rider just pedals.
No copy-paste, no second terminal.

Each step prints a one-line label so the rider knows what the trainer
is about to do next. Inbound envelopes are also printed as they
arrive, so anomalies (rejections, missing acks) are visible in the
same terminal.

Pair with ``run-live-test.ps1 -Record`` so the sent commands + sidecar
acks all land in the JSONL for post-ride analysis.

Usage (invoked by run-smoke-pause-sim.ps1):
    python scripts/_smoke_pause_sim.py [ws-url]
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import websockets

DEFAULT_URL = "ws://localhost:8421"

# Each step is (wait_seconds_before_send, command_or_None, label).
# wait_s gives the rider time to react to the previous step before
# the next one fires.
STEPS: list[tuple[float, dict[str, Any] | None, str]] = [
    (5.0, None, "warming up — pedal at ~80 rpm; first command fires in 5s"),
    (0.0, {"type": "set_target_power", "watts": 150}, "▶ baseline ERG at 150W"),
    (15.0, None, "hold 150W for 15s"),
    (
        0.0,
        {"type": "pause", "reason": "smoke", "target_watts": 50},
        "▶ structured pause to 50W easy spin",
    ),
    (10.0, None, "easy spin for 10s — keep pedaling, trainer is at 50W"),
    (
        0.0,
        {"type": "set_target_power", "watts": 200},
        "▶ set_target_power 200W while paused (should be deferred)",
    ),
    (5.0, None, "5s — sidecar should reject with reason='deferred-paused'; trainer stays at 50W"),
    (0.0, {"type": "resume"}, "▶ resume — sidecar ramps from 50W up to the deferred 200W target"),
    (15.0, None, "15s — ride the ramped 200W ERG target"),
    (
        0.0,
        {"type": "set_simulation", "grade_percent": 5.0},
        "▶ switch to SIM mode — 5% grade (you control power via gear/cadence)",
    ),
    (20.0, None, "20s — ride the 5% climb; shift/change cadence as you would outdoors"),
    (0.0, {"type": "set_simulation", "grade_percent": 9.0}, "▶ steeper — 9% grade"),
    (15.0, None, "15s — ride the steeper climb"),
    (
        0.0,
        {"type": "set_simulation", "grade_percent": -3.0},
        "▶ descent — -3% grade (trainer feels easy)",
    ),
    (10.0, None, "10s — easy descent; spin out"),
    (0.0, {"type": "set_target_power", "watts": 150}, "▶ back to ERG at 150W"),
    (10.0, None, "10s — verify ERG holds while you pedal at varying cadence"),
    (0.0, None, "✓ smoke sequence complete — Ctrl+C the sidecar to flush the recording"),
]


async def _reader(ws: websockets.WebSocketClientProtocol) -> None:
    """Print interesting inbound envelopes so the rider sees acks live."""
    interesting = {
        "target_power_set",
        "paused",
        "resumed",
        "simulation_set",
        "cadence_bailout_engaged",
        "cadence_bailout_disengaged",
    }
    async for message in ws:
        try:
            env = json.loads(message)
        except json.JSONDecodeError:
            continue
        type_ = env.get("type")
        if type_ in interesting:
            data = env.get("data", {})
            # Compact one-line summary
            summary = ", ".join(f"{k}={v}" for k, v in data.items() if not isinstance(v, dict))
            print(f"  ◀ {type_}: {summary}")


async def _runner(url: str) -> None:
    print(f"\nconnecting to {url} ...")
    async with websockets.connect(url) as ws:
        reader_task = asyncio.create_task(_reader(ws))
        try:
            for wait_s, cmd, label in STEPS:
                if wait_s > 0:
                    print(f"  ⏳ {wait_s:>3.0f}s  {label}")
                    await asyncio.sleep(wait_s)
                else:
                    print(f"        {label}")
                if cmd is not None:
                    await ws.send(json.dumps(cmd))
        finally:
            reader_task.cancel()


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print("=" * 70)
    print("PAUSE + SIM SMOKE RUNNER")
    print("=" * 70)
    print("Pre-flight checklist (in your sidecar terminal):")
    print("  1. run-live-test.ps1 -Record is running")
    print("  2. log shows 'matched FTMS Bike → KICKR ...'")
    print("  3. log shows 'KICKR ... claimed control — set_target_power now active'")
    print("  4. you're on the bike, ready to pedal at ~80 rpm")
    print()
    input("Press Enter when ready ...")
    print()
    try:
        asyncio.run(_runner(url))
    except KeyboardInterrupt:
        print("\nstopped early.")


if __name__ == "__main__":
    main()
