# HANDOFF — roguERGlike-sidecar

> Current state of this repo. Updated at the end of every session that touches it. Read first.

**Last updated:** 2026-05-20
**Last session log:** `../../docs/sessions/2026-05-20-phase-2-ble-ftms.md` (in umbrella; immediately follows `2026-05-20-bike-integration-handshake.md` from earlier the same day)
**Current branch:** `feat/ble-ftms` (PR #4) + `docs/handoff-phase-2-ble` (this PR)
**Current focus:** Phase 2 (real BLE) shipped behind 30/30 unit tests; the very next thing is connecting to an actual FTMS trainer to live-validate before merging PR #4.

## Where we are

Phase 1 mock mode is merged on `develop`. Phase 2 introduces a tiny "profile registry" architecture: each BLE device type is a pure `BleProfile` (service_uuid + char_uuid + device_kind + `decode(bytes) → events`), and a `BleSource` per profile-plus-address feeds the shared `EventBus`. The first profile, `FtmsBikeProfile`, decodes FTMS Indoor Bike Data (service `0x1826`, characteristic `0x2AD2`) into power / cadence / speed / heart_rate events. The CLI gains `--scan` (discover and exit) and `--mode live --device-bike <name|addr>` (connect and stream).

30/30 unit tests pass (17 new) including 9 handcrafted FTMS packet fixtures covering: power-only, full bike (speed+cadence+power+HR), unsupported-fields-skipped alignment, negative-power clamping, zero-HR drop, empty / truncated payloads, profile metadata. Mock mode wire output is byte-identical before and after the `session.py` refactor — confirmed via a freshly-started sidecar + a `websockets.connect` probe.

What is **not** done: connecting to a real trainer over BLE. Hardware is in the room but wasn't paired this session.

## What's next (immediate)

1. **Pair the FTMS trainer to Windows Bluetooth.** Once paired, from this repo: `roguerglike-sidecar --scan` (should list the trainer with its name + BLE address + RSSI), then `roguerglike-sidecar --mode live --device-bike "<name-substring>"`. In parallel, run engine `test_main` headless and confirm real `power_changed` / `cadence_changed` flow with realistic values while pedalling. Fix anything that surprises (decoder quirks for this specific trainer, reconnect flakiness, etc.) in PR #4 before merge.
2. **Merge PR #4** (Phase 2 BLE), then this docs PR.
3. **HRS profile** on `feat/ble-hrs`. Adds `HrsProfile` (service `0x180D`, characteristic `0x2A37`) + `--device-hr` flag + HR de-dup policy (default: prefer standalone chest strap over bike-embedded HR; `--prefer-bike-hr` override).

## Open threads

- **Hardware validation pending for Phase 2.** See #1 above. Until that runs, PR #4 is "unit-tested only".
- **Distance not yet emitted.** FTMS decoder consumes the `meters_total` field but emits no `DistanceData` because the schema requires `meters_delta`, which needs a stateful deriver outside the pure decoder. Small follow-up (`feat/distance-deriver`).
- **`--mode live` does not start the slider UI** — by design. Consider a small status / diagnostic page later (connection state + most recent packet timestamps + RSSI), reusing the aiohttp server.
- **HR de-dup policy** — decision deferred until HRS profile lands.
- **`fit-tool` is an unused runtime dep** for now. Carrying it for the eventual FIT export; trim if YAGNI bites.
- **Bleak version pin** at `>=0.22.0` — watch for adapter / OS quirks on Linux / macOS CI runners (Windows is where Bleak is most mature).
- **CI matrix is real.** Phase 2 brings actual BLE imports; verify ubuntu-latest / macos-latest jobs handle Bleak's optional D-Bus / CoreBluetooth deps cleanly. If they don't, gate the BLE imports behind a `try/except ImportError` or mark Bleak-touching tests with a skip when no adapter is present (current tests don't touch Bleak directly, so this may not bite).

## Notes for next session

- The event schema (`docs/event-schema.md`) is a stable contract. Breaking changes require a major version bump and coordinated update in `repos/engine`.
- The profile model is documented in `docs/ble-profiles.md` — read this before adding HRS or any new device type.
- Pre-commit hook + local ruff now agree on import grouping (fixed in pyproject via `known-first-party`). If a commit still aborts with "files were modified by this hook", investigate before assuming flap.
- `Get-NetTCPConnection -LocalPort 8421,8422` + `Stop-Process` if a fresh sidecar start refuses to bind because a previous run leaked.

## Entry point for next session

> "Pair the FTMS trainer to Windows Bluetooth, run `roguerglike-sidecar --scan` to confirm discovery, then `roguerglike-sidecar --mode live --device-bike '<name-substring>'` alongside the engine's `test_main` headless. Confirm real power/cadence events flow with realistic values, fix anything that surprises in PR #4, then merge. After merge, start HRS profile on `feat/ble-hrs`."
