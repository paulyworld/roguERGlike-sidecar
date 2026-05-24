# HANDOFF — roguERGlike-sidecar

> Current state of this repo. Updated at the end of every session that touches it. Read first.

**Last updated:** 2026-05-23
**Last session log:** `../../docs/sessions/2026-05-22-first-live-ride-and-pause-architecture.md` (in umbrella)
**Current branch:** `feat/record-flag`
**Current focus:** **Full trainer-control loop validated end-to-end against the KICKR + Whoop.** All three bailout-related bugs shipped and confirmed working in a real ride. Today: first-class `--record` flag landing; semantic annotations primitive being planned next; then engine-side pause command (Pattern B) when engine #4 is promoted.

## Where we are

The sidecar now does the full job, validated in a live ride 2026-05-22:

- **Mock mode** (slider UI for off-bike dev) — wire-equivalent to live mode.
- **FTMS bike trainer** (service 0x1826) — power, cadence, speed live; FTMS Control Point (0x2AD9) writes for ERG mode.
- **HR Sensor (HRS)** (service 0x180D) — chest straps, smartwatches, Whoop MG5 (broadcast HR enabled).
- **FTMS feature discovery** (`device_capabilities` event) so the engine knows whether `target_power` is supported before attempting writes.
- **Cadence-based ERG bailout** with intensity-aware curves: at low intensity the bailout is patient (~90s at <50% FTP), at high intensity it's aggressive (~15s at ≥150% FTP). Resume ramp inverts the curve.
- **Silent-failure surfacing**: missing `--allow-trainer-control` now publishes `target_power_set accepted=false reason="trainer-control disabled"` instead of dropping the command. Bailout-active rejections surface as `accepted=false reason="bailout-pending"`.

Validated against a Wahoo KICKR CORE 1003 + Whoop MG5. Three concurrent sources via `asyncio.gather`. Engine pushed real ERG targets through the loop; bailout engaged and disengaged correctly during natural between-round pauses (math: 74s at 177W with FTP 250 → pct_ftp 0.71 → expected 74.4s ✓).

113 unit tests passing. Ruff + mypy clean.

## What shipped today (2026-05-22)

- **PR #16/#17 (squash-bundled)** — three bug fixes uncovered during the first attempted live ride: (A) bailout `_last_active_ts` resets on first meaningful ERG target so the idle window starts at "first real workout" not "sidecar startup"; (B) forgiving scan loop with `--scan-timeout-s` (default 30s) that preserves already-matched devices across cycles and logs what's still missing; (C) explicit `INFO ... claimed control of <device>` log line so operators have a positive signal independent of the trainer LED.
- **PR #17** — `scripts/run-live-test.ps1` PowerShell launcher with splatting-based args (paste-safe alternative to backtick continuation).
- **PR #18** — `docs/architecture/safety-vs-pause.md` documenting that cadence bailout is safety, not game flow. Two valid pause patterns: client-side soft target (Pattern A, shipped in concert-mvp) and sidecar-side suspend command (Pattern B, planned).
- **PR #19** — `scripts/record_session.py` + `record-session.ps1` wrapper. WS subscriber writing every envelope to `docs/recordings/<timestamp>.jsonl` (gitignored). Continuously flushed.

## What's next (immediate)

1. **Pattern B pause primitive** (when engine asks for it). Small: new `PauseCommand` / `ResumeCommand` discriminated-union pair in `events.py`; handler in `cli.py` + `mock.py` that toggles `CadenceBailout.set_paused(...)` and optionally sets a configurable easy-spin wattage; typed `paused` / `resumed` envelopes. `CadenceBailout` already has internal `_paused` plumbing — wire to it.
2. **Distance deriver** on `feat/distance-deriver`. FTMS Indoor Bike Data carries `meters_total`; small stateful module tracks `last_total → meters_delta` per session and emits `DistanceData` events.
3. **CSCS profile** on `feat/ble-cscs`. Cycling Speed and Cadence Service (0x1816) for older trainers / power meters that expose cadence outside FTMS. Same shape as HRS.
4. ~~First-class `--record <path>` flag on the CLI~~ — landed on `feat/record-flag`. `scripts/record_session.py` + `record-session.ps1` removed. `run-live-test.ps1` gained `-Record` / `-RecordPath`.

5. **Semantic annotations (`RiderAnnotation` event + `annotate` command).** New typed command/envelope pair so clients (concert-mvp first via F2) can mark moments during a ride — "ui-pause", "felt-unfair", "bug-here". Persists alongside telemetry in the same JSONL, with sidecar `ts`, so post-ride debugging of bailout/pause becomes self-annotating. See umbrella session log for the design rationale.

Other candidates (non-blocking):

- Reconnect-on-drop chaos test (yank trainer power mid-stream).
- Status/diagnostic page in live mode (connection state + RSSI + latest packet ts) reusing aiohttp.
- Pairing UI (Phase 3 per `device-pairing-ux-model` memory) — Zwift-style web pairing + persistent device config.
- Quiet the websockets `EOFError` traceback noise when a TCP connection probes the port without sending HTTP. Cosmetic only; the server is unaffected.

## Open threads

- **Whoop broadcast HR is per-activity.** The MG5 resets broadcast mode when the user starts/stops an activity in the app; if scans don't find MUDRAT-DETECTOR, the rider needs to restart the activity. Worth documenting in `docs/ble-profiles.md`'s HRS section.
- **Cadence bailout watcher runs continuously**, even when concert-mvp's client-side soft pause is active. A long video pause at low intensity can still trip the bailout — clients must handle `bailout-pending` rejections gracefully. Documented in `docs/architecture/safety-vs-pause.md`.
- **`fit-tool`** runtime dep is still unused; carried for eventual FIT export. Trim if YAGNI bites.
- **Bleak version pin** at `>=0.22.0`. Windows is where Bleak is most mature; Linux/macOS CI not stress-tested.

## How to test live

```powershell
# Sidecar with built-in JSONL recording (single command — no second terminal needed)
./scripts/run-live-test.ps1 -Record                          # auto-stamped path under docs/recordings/
./scripts/run-live-test.ps1 -RecordPath rides/hiit-test.jsonl

# Other variants
./scripts/run-live-test.ps1                                  # defaults: KICKR + mudrat + FTP 250 + scan 60s + ERG control
./scripts/run-live-test.ps1 -RiderFtp 275
./scripts/run-live-test.ps1 -Mock                            # off-bike dev path
```

Success markers in the sidecar log (in order): `matched FTMS Bike → ...` / `matched HR Sensor → ...` → `device_connected ...` → `device_capabilities supports_target_power=true` → `<device>: claimed control — set_target_power now active` → `target_power_set accepted=true` once the engine writes.

## Entry point for next session

> "`--record` flag shipped (feat/record-flag); replaces the external WS-subscriber recorder. Next track is the semantic-annotations primitive (`RiderAnnotation` event + `annotate` command) — small contract addition, validates the F2 mid-ride annotation UX on concert-mvp afterward. Alt tracks still open: Pattern B pause command (gated on engine #4 promotion), distance deriver, CSCS profile."
