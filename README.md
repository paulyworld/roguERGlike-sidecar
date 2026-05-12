# roguERGlike-sidecar

> Bluetooth bridge and telemetry pipeline for connected fitness equipment.

A standalone Python service that connects to BLE fitness devices, normalizes their data into a common event stream, and emits clean JSON over a local WebSocket. Designed as the sensor layer for the `roguERGlike` family of games, but useful as a general-purpose fitness telemetry tool.

## Supported devices

**Phase 1 (current focus):**
- FTMS-compatible smart bike trainers (Wahoo Kickr, Tacx, Elite, Saris, JetBlack, etc.)
- Standard BLE heart rate sensors (Polar, Wahoo Tickr, Garmin, Apple Watch broadcast mode, etc.)

**Planned:**
- Rowing ergs — Concept2 PM5 (proprietary BLE), FTMS Rower-compliant devices
- Treadmills — FTMS Treadmill
- Cross-trainers / ellipticals — FTMS Cross-Trainer
- Standalone power meter pedals — Cycling Power Service

The design goal is that **the event schema is the same regardless of device.** A "sprint" on a bike, a "power 10" on a rower, and a sustained sprint pace on a treadmill all surface as the same `effort_surge` event with normalized power. Game logic stays device-agnostic; only the input layer changes.

## Features

- BLE connection to FTMS devices and standard BLE HR sensors
- Normalized JSON event stream over local WebSocket (`ws://localhost:8421`)
- Derived signal layer: surge detection, cadence/stroke steadiness, HR zone transitions
- Three operating modes:
  - **Live** — real sensors via Bleak
  - **Replay** — stream a recorded session at real-time speed
  - **Mock** — slider-driven web UI for off-equipment development
- Continuous session recording (append-only JSONL)
- Export to FIT, TCX, and GPX
- Cross-platform: Windows, macOS, Linux (mobile via separate companion later)

## Quickstart

```bash
# Requires Python 3.11+
pip install -e .
roguerglike-sidecar --mode mock          # slider UI at http://localhost:8422
roguerglike-sidecar --mode live          # scan and connect to real devices
roguerglike-sidecar --mode replay --file rides/example.jsonl
```

## Event schema

See [`docs/event-schema.md`](docs/event-schema.md). Stable contract — game code depends on it. Device-specific events use neutral names (`power`, `cadence`) where possible; device-only signals (e.g. `stroke_rate` for rowers) are added as new event types rather than overloading existing ones.

## Status

Pre-alpha. Building in public. See [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).

Contributions welcome — especially device compatibility reports and PRs adding support for new equipment. By contributing you agree to the [CLA](CLA.md).
