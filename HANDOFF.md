# HANDOFF — roguERGlike-sidecar

> Current state of this repo. Updated at the end of every session that touches it. Read first.

**Last updated:** 2026-05-21
**Last session log:** `../../docs/sessions/2026-05-21-hrs-validation-and-mvp-promotion.md` (in umbrella)
**Current branch:** `docs/post-hrs-mvp-promotion-sidecar` (PR pending); develop has the full cycling BLE pipeline
**Current focus:** **Full cycling BLE pipeline shipped and live-validated.** Mock + FTMS bike + HRS heart-rate all on develop; concurrent multi-source live mode confirmed against a KICKR CORE 1003 + Whoop MG5. Next: distance deriver, CSCS, or session recording.

## Where we are

Three BLE source paths now ship and have been validated against real hardware:

- **Mock mode** (slider UI for off-bike dev) — wire-equivalent to live mode
- **FTMS bike trainer** (service 0x1826) — live against Wahoo KICKR CORE 1003
- **HR Sensor (HRS)** (service 0x180D) — live against Whoop MG5 with Broadcast HR enabled

All three feed the same `EventBus` and emit identical wire envelopes. `--mode live --device-bike "KICKR" --device-hr "mudrat"` runs both BLE sources concurrently via `asyncio.gather` — confirmed working with the engine's MVP HIIT loop receiving power, cadence, and HR simultaneously and reacting in real time.

HR de-duplication: when both a bike (FTMS sometimes embeds HR) and a standalone HR sensor are paired, the standalone wins by default (the bike source publishes everything *except* `heart_rate`). `--prefer-bike-hr` inverts. Policy lives in `_resolve_drop_types` in `cli.py` — pure function, fully unit-tested.

51/51 unit tests pass; ruff + mypy clean. Two memories captured during this run: `bleak-windows-scan-filter-bug` (always scan unfiltered + post-filter; Bleak's filter is unreliable on Windows) and `hr-source-breadth` (build via standard BLE HRS; document what does and doesn't speak it).

## What's next (immediate)

1. **Distance deriver** on `feat/distance-deriver`. FTMS Indoor Bike Data carries `meters_total` and the decoder already consumes those bytes; this branch adds a small stateful module (e.g. `ble/distance_deriver.py`) that tracks `last_total → meters_delta` per session and emits `DistanceData` events. Outside the pure decoder by design.
2. **CSCS profile** on `feat/ble-cscs`. Cycling Speed and Cadence Service (`0x1816`) for older trainers / power meters that expose cadence outside FTMS. Same profile-module shape as HRS; small.
3. **Session recording.** Append-only JSONL writer subscribed to `EventBus`. Foundation for replay mode and FIT export. Probably its own `recording/` package.

Other candidates (not blocking):
- Status/diagnostic page in live mode (connection state per source + latest packet ts + RSSI) reusing the aiohttp server.
- Reconnect-on-drop chaos test.
- Pairing UI (~Phase 3 per `device-pairing-ux-model` memory) — Zwift-style web pairing + persistent device config.

## Open threads

- **`fit-tool`** runtime dep is unused for now; carried for eventual FIT export. Trim if YAGNI bites.
- **Bleak version pin** at `>=0.22.0`. Windows is where Bleak is most mature; CI matrix on Linux/macOS hasn't been exercised heavily.
- **Reconnect-on-drop** in `BleSource.run` has a 5-second backoff and infinite retry. Hasn't been stress-tested by yanking trainer power mid-stream; worth a chaos test.
- **HRS validated against Whoop MG5 only** so far. Other HRS-broadcasting devices (Polar / TICKR / Garmin / Suunto / smartwatches with broadcast apps) are unit-tested via the spec but not live-confirmed in this project — add to the compatibility list in `docs/ble-profiles.md` as you encounter them.
- **First CI runs** still subject to the `github-actions-first-push-quirk` memory.

## Notes for next session

- The event schema (`docs/event-schema.md`) is a stable contract. Breaking changes require a major version bump and coordinated update in `repos/engine`.
- The profile model is documented in `docs/ble-profiles.md` — read this before adding a new device type. The "HR Sensor compatibility" and "HR de-duplication policy" subsections capture the breadth-and-policy lessons from this session.
- When pairing a new BLE device for testing: the device's advertised BLE name can be **anything** the user has set in the vendor app (e.g. Whoop MG5 advertising as "MUDRAT-DETECTOR"); don't assume vendor strings appear in the name. Discover by service UUID via `--scan`.
- Pre-commit + local ruff agree on import grouping via `tool.ruff.lint.isort.known-first-party`. If a commit aborts with "files were modified by this hook", something else changed — investigate before assuming flap.
- KICKR LED: solid blue = one BLE host (us) connected; blinking = advertising. Use as fast diagnostic.

## Entry point for next session

> "Pick one: (a) distance deriver on `feat/distance-deriver` — stateful `last_total → meters_delta` outside the pure FTMS decoder, emitting `DistanceData` events; (b) CSCS profile on `feat/ble-cscs` for older trainers / power meters with cadence outside FTMS, same module shape as HRS; (c) session recording on `feat/session-recording` — append-only JSONL writer subscribed to EventBus, foundation for replay + FIT export."
