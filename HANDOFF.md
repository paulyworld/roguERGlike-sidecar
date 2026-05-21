# HANDOFF — roguERGlike-sidecar

> Current state of this repo. Updated at the end of every session that touches it. Read first.

**Last updated:** 2026-05-21
**Last session log:** `../../docs/sessions/2026-05-21-hardware-validation-and-merges.md` (in umbrella)
**Current branch:** `docs/post-phase-2-merge-sidecar` (PR pending); feature work all merged
**Current focus:** Phase 2 (real BLE) merged on develop and **live-validated against a KICKR CORE 1003**. Next: HRS profile (chest-strap HR) or distance deriver.

## Where we are

Phase 1 mock mode + Phase 2 FTMS bike trainer are both merged. The CLI supports `--mode mock` (slider UI for development), `--mode live --device-bike <name|addr>` (real FTMS trainer), and `--scan` (list discoverable FTMS devices and exit). 30/30 unit tests pass; ruff + mypy clean; pre-commit + CI hooks healthy. The pipeline is proven end-to-end against a real KICKR CORE 1003.

A subtle but important fix landed during validation: `BleakScanner.discover(service_uuids=)` on Windows silently drops devices that advertise the target UUID in their scan response rather than the initial advertisement packet (KICKRs do this). The scanner now does an unfiltered scan and post-filters in Python — captured as memory `bleak-windows-scan-filter-bug` so the same trap doesn't catch the HRS / CSCS profiles.

## What's next (immediate)

1. **HRS profile** on `feat/ble-hrs`. Module: `src/roguerglike_sidecar/ble/hrs.py` with `HrsProfile` (service `0x180D`, characteristic `0x2A37` Heart Rate Measurement). Decoder is small: a flags byte then a u8 or u16 BPM. Add `--device-hr <name|addr>` flag and start a second concurrent `BleSource` so bike + strap work together. Land the HR-source dedup policy as part of this PR (default: prefer standalone HR sensor over bike-embedded HR; `--prefer-bike-hr` override).
2. **Distance deriver** on `feat/distance-deriver`. The FTMS decoder already consumes the `meters_total` field but emits no `DistanceData` because the schema requires `meters_delta`. Add a tiny stateful deriver *outside* the pure decoder (`ble/distance_deriver.py` or similar) that tracks the previous total per session and emits `DistanceData(meters_total, meters_delta)` events on each FTMS packet that carries distance.
3. **Session recording.** Append-only JSONL writer subscribed to `EventBus`. Foundation for replay mode and FIT export. Could come before or after HRS.

## Open threads

- **`--mode live` does not start the slider UI** — by design. Consider a small status / diagnostic page later (connection state + latest packet ts + RSSI), reusing the aiohttp server.
- **HR de-dup policy** — decision deferred to the HRS PR. Default plan: prefer standalone HR sensor when both present; `--prefer-bike-hr` override.
- **`fit-tool` is an unused runtime dep** for now. Carrying it for eventual FIT export; trim if YAGNI bites.
- **Bleak version pin** at `>=0.22.0` — watch for adapter / OS quirks on Linux / macOS CI runners (Windows is where Bleak is most mature). CI didn't fire on the merged PRs (first-push quirk per memory); next push should be the first real CI run.
- **Reconnect-on-drop** in `BleSource.run` has a 5-second backoff and an infinite retry loop. Has not yet been stress-tested by yanking the trainer's power mid-stream; worth a manual chaos test soon.

## Notes for next session

- The event schema (`docs/event-schema.md`) is a stable contract. Breaking changes require a major version bump and coordinated update in `repos/engine`.
- The profile model is documented in `docs/ble-profiles.md` — read this before adding HRS or any new device type.
- Pre-commit hook + local ruff agree on import grouping via `tool.ruff.lint.isort.known-first-party = ["roguerglike_sidecar"]` in pyproject. If a commit still aborts with "files were modified by this hook", investigate before assuming flap.
- `Get-NetTCPConnection -LocalPort 8421,8422` + `Stop-Process` if a fresh sidecar start refuses to bind because a previous run leaked.
- When testing live mode: KICKR's blue LED is **solid** when one BLE host (us) is connected, **blinking** when advertising. The light is a fast diagnostic.

## Entry point for next session

> "Add `HrsProfile` (service 0x180D, characteristic 0x2A37) on `feat/ble-hrs`: one module + decoder + unit tests against handcrafted HR packets + `--device-hr <name|addr>` CLI flag wiring a second concurrent `BleSource`. Define the HR-source dedup policy as part of this PR. Apply the `bleak-windows-scan-filter-bug` pattern (unfiltered scan + post-filter) from the start."
