# CLAUDE.md — roguERGlike-sidecar

You're working in `roguERGlike-sidecar`, one of four repos in the roguERGlike project.

## Read first

1. `HANDOFF.md` (this directory) — current state of this repo
2. `../../INSTRUCTIONS.md` (in the umbrella) — canonical project-wide conventions
3. The umbrella's `HANDOFF.md` if your work touches multiple repos

## This repo's role

Bluetooth bridge and telemetry pipeline. Connects to FTMS-compatible smart trainers and BLE heart rate sensors, normalizes their data into a device-agnostic event stream, and emits JSON over a local WebSocket. Also handles continuous session recording and export to FIT/TCX/GPX.

**Visibility:** public
**License:** MIT

## In scope

- BLE communication with fitness devices (currently FTMS bike trainers + HR sensors; future: rowers, treadmills)
- Normalized JSON event schema (see `docs/event-schema.md`)
- WebSocket server emitting events to local clients (the game engine)
- Mock mode (slider-driven web UI) for off-equipment development
- Replay mode (stream recorded sessions at real-time speed)
- Session recording to append-only JSONL
- Export to FIT, TCX, GPX

## Out of scope

- Game logic (lives in `repos/engine` and `repos/game`)
- BLE protocols other than FTMS / HRS / standard cycling services (for now — additions welcome via PR)
- Cloud sync, accounts, online features (those live in `repos/server` eventually)
- Mobile native BLE — handled by a separate companion later

## House conventions

- Follow the umbrella `INSTRUCTIONS.md` for sessions, branches, commits, versioning
- Branch off `develop`, never push to `main` or `develop` directly
- Sign commits (`git commit -S`)
- Python 3.11+; use `ruff` for lint and format; `pytest` for tests
- Run `pre-commit install` once after cloning

## Tech-specific notes

- **Bleak** is the BLE library — async-first, cross-platform (Windows/Mac/Linux)
- **websockets** for the WS server
- **fit-tool** for FIT export
- **Pydantic v2** for event schema validation
- **Click** for CLI

The event schema (`docs/event-schema.md`) is a **stable contract** the game depends on. Breaking changes require a major version bump and a coordinated update in `repos/engine`.

## Common tasks

```bash
# Install for development
pip install -e ".[dev]"
pre-commit install

# Run in mock mode (no real device needed)
roguerglike-sidecar --mode mock

# Scan for all known BLE profiles (FTMS bike + HR sensor) in range and exit
roguerglike-sidecar --scan

# Run against a real bike trainer (name substring or BLE address)
roguerglike-sidecar --mode live --device-bike "KICKR"

# Run against a bike + chest strap (strap is the HR source of truth)
roguerglike-sidecar --mode live --device-bike "KICKR" --device-hr "Polar"

# Same, but use the bike's embedded HR instead of the strap
roguerglike-sidecar --mode live --device-bike "KICKR" --device-hr "Polar" --prefer-bike-hr

# HR-only run (no bike)
roguerglike-sidecar --mode live --device-hr "TICKR"

# Opt in to trainer control (FTMS Control Point writes — ERG mode).
# Aggressive defaults per project memory: 0-800W cap; 10s disconnect bailout.
roguerglike-sidecar --mode live --device-bike "KICKR" --allow-trainer-control

# Same, with a more conservative wattage cap and a 5s disconnect bailout.
roguerglike-sidecar --mode live --device-bike "KICKR" --allow-trainer-control \
    --max-target-power 400 --disconnect-bailout-s 5

# Trainer control + intensity-aware cadence bailout (recommended for real rides).
# With --rider-ftp set, the bailout wait + resume ramp adapt to current % FTP:
# patient at low intensity, fast at high intensity (per intensity-aware-safety-curves).
roguerglike-sidecar --mode live --device-bike "KICKR" --device-hr "mudrat" \
    --allow-trainer-control --rider-ftp 250

# Mock mode with trainer control accepted — useful for engine ERG dev off-bike.
roguerglike-sidecar --mode mock --allow-trainer-control

# Record every published envelope to JSONL for post-ride analysis.
# Includes the session-state replay so the file is self-contained.
roguerglike-sidecar --mode live --device-bike "KICKR" --device-hr "mudrat" \
    --allow-trainer-control --rider-ftp 250 \
    --record docs/recordings/test.jsonl

# Replay a recorded session  (NOT YET IMPLEMENTED — Phase 3)
roguerglike-sidecar --mode replay --file rides/example.jsonl

# Lint + test
ruff check . && pytest
```
