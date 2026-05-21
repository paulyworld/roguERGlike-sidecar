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

# Scan for FTMS bike trainers in range and exit
roguerglike-sidecar --scan

# Run against a real bike trainer (name substring or BLE address)
roguerglike-sidecar --mode live --device-bike "KICKR"

# Replay a recorded session  (NOT YET IMPLEMENTED — Phase 3)
roguerglike-sidecar --mode replay --file rides/example.jsonl

# Lint + test
ruff check . && pytest
```
