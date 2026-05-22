# HANDOFF — roguERGlike-sidecar

> Current state of this repo. Updated at the end of every session that touches it. Read first.

**Last updated:** 2026-05-21
**Last session log:** `../../docs/sessions/2026-05-21-ftms-erg-end-to-end.md` (in umbrella)
**Current branch:** `docs/post-trainer-control-end-to-end-sidecar` (PR pending); develop has the full trainer-control loop
**Current focus:** Full FTMS trainer-control feature shipped (capability discovery + control writes + replay fix). KICKR live-validated for read AND write via Python WS probe. **Engine-driven ERG validation is the next live test** (engine repo).

## Where we are

Three trainer-control PRs landed on develop this session:

- **PR #9** — FTMS capability discovery. Reads `0x2ACC` Feature characteristic at connect, emits `device_capabilities` event with target_power / target_resistance / target_inclination / target_heart_rate / indoor_bike_simulation flags. Engine gates ERG UI on `target_power: true`.
- **PR #11** — FTMS Control Point writes (re-landed after a stacked-PR retarget snafu). `FtmsControl` state machine (Request Control → Start → Set Target Power), bidirectional WS (engine ↔ sidecar over the same connection), `--allow-trainer-control` opt-in flag, aggressive-default safety bounds (per memory), disconnect-bailout watcher, mock-mode ERG simulation that mirrors the live wire envelopes. Live-validated against KICKR CORE 1003 via a Python WS probe — 100 → 250 W ramp, every command `accepted=true`, rider reported the resistance steps felt clean.
- **PR #12** — Replay control-lifecycle events to late subscribers. Widens `SESSION_STATE_TYPES` to include `device_capabilities`, `control_acquired`, `control_released`. `target_power_set` deliberately NOT replayed (it's an ack, not state). Caught while smoke-testing engine PR #7's bridge against mock mode.

Also new: `scripts/record_session.py` — WS subscriber that dumps every envelope to a timestamped JSONL under `docs/recordings/` (gitignored). Pairs with the screenshot pattern for live-validation evidence.

94/94 unit tests pass; ruff + mypy clean. Three project memories now shape future work in this repo: `bleak-windows-scan-filter-bug`, `hr-source-breadth`, `trainer-control-aggressive-defaults`.

## What's next (immediate)

1. **Engine-driven ERG live validation** (paired with the engine repo's PR #4). Run sidecar with `--allow-trainer-control --disconnect-bailout-s 900`, the JSONL recorder alongside, engine in the `engine-mvp` worktree. Confirm the MVP loop's per-phase `set_target_power` calls arrive as `target_power_set accepted=true` and the trainer's resistance tracks intended phases. Save the JSONL + a screenshot as validation evidence.
2. **`--record <path>` as a first-class flag.** Replaces the external `scripts/record_session.py` with a sidecar-side recorder that subscribes to its own bus. The script is a fine interim tool but the in-process version is cleaner and doesn't require a second BLE-free Python process. Small follow-up PR.
3. **FTMS SIM mode.** Set Indoor Bike Simulation Parameters (opcode `0x11`) for slope/wind/CRR. Gated on `indoor_bike_simulation: true`. Useful when a non-HIIT mechanic wants the trainer to feel like a hill rather than ERG-locked at a wattage.

Other candidates (not blocking):
- Distance deriver (FTMS decoder consumes `meters_total` but emits no `DistanceData` because `meters_delta` needs a stateful deriver outside the pure decoder).
- CSCS profile (older trainers / power meters with cadence outside FTMS).
- Status / diagnostic page in live mode (connection state per source + latest packet ts + RSSI), reusing the aiohttp server.
- Reconnect-on-drop chaos test (yank trainer power mid-stream).

## Open threads

- **Disconnect-bailout default** (`--disconnect-bailout-s 10`) is too short for "set up sidecar, then engine, then ride" — every live test bumps it to 600+. Might revisit the default. The behaviour is correct; only the magnitude is unfriendly.
- **`fit-tool` runtime dep is unused** for now. Carrying it for the eventual FIT export. Trim if YAGNI bites.
- **Bleak version pin** `>=0.22.0`. Windows is where Bleak is most mature; CI matrix on Linux/macOS hasn't been heavily exercised.
- **HRS validated against Whoop MG5 only**. Other HRS-broadcasting devices (Polar / TICKR / Garmin / etc.) are unit-tested via the spec but not live-confirmed in this project. Add to the compatibility table in `docs/ble-profiles.md` as encountered.
- **GitHub stacked-PR retarget gotcha** (lesson from this session): GitHub does NOT auto-retarget a child PR when its parent merges if the parent branch is not deleted. Re-base onto develop + re-PR if it happens again. Or enable "delete branch on merge" at the repo level.
- **First CI runs** still subject to the `github-actions-first-push-quirk` memory on some PRs.

## Notes for next session

- Read `docs/event-schema.md` if anything you're doing touches the wire — the contract is stable now and covers both outbound events AND inbound commands (the Trainer Control section was added this session).
- Read `docs/ble-profiles.md` before adding a new device type. The "Trainer control (write side)" subsection captures the FtmsControl architecture; the HRS compatibility table is the reference for what HR devices we know work.
- For live tests: KICKR LED diagnostic is solid blue = sidecar has it; blinking = available. Quick eyeball check.
- `Get-NetTCPConnection -LocalPort 8421,8422` + `Stop-Process` if a fresh sidecar refuses to bind because a previous run leaked.

## Entry point for next session

> "Pair with the engine repo for the engine-driven ERG live validation against KICKR + Whoop. Sidecar: `--mode live --device-bike KICKR --device-hr mudrat --allow-trainer-control --disconnect-bailout-s 900`. JSONL recorder: `python scripts/record_session.py`. Engine: ▶ Play on the engine-mvp worktree's MVP scene. Confirm `target_power_set accepted=true` lines flow in the sidecar log as the engine drives each phase, and the JSONL contains the full event stream. Then either land the `--record <path>` first-class flag, or move on to FTMS SIM mode."
