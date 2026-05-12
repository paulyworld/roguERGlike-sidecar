# HANDOFF — roguERGlike-sidecar

> Current state of this repo. Updated at the end of every session that touches it. Read first.

**Last updated:** 2026-05-12
**Last session log:** `../../docs/sessions/2026-05-12-repo-setup-and-conventions.md` (in umbrella)
**Current branch:** none — not yet `git init`'d
**Current focus:** Repo not yet bootstrapped.

## Where we are

Skeleton files exist (README, LICENSE, CLA, CONTRIBUTING, CHANGELOG, pyproject.toml, pre-commit config, CI workflow, event schema doc, empty src package). No actual implementation yet. No `git init`.

## What's next (immediate)

1. `git init -b main`, first commit, push to GitHub as `roguERGlike-sidecar` (public)
2. Set up branch protection on `main` (require PR + CI)
3. Begin Phase 1: **mock mode** implementation
   - Pydantic models for the event schema
   - WebSocket server emitting normalized events
   - Web UI with sliders for power / cadence / HR
   - Verify the engine's `effort_bridge.gd` receives events correctly

## Open threads

- Real BLE work (Bleak) comes after mock mode is stable
- FIT export comes after live mode is stable
- Replay mode comes after at least one real ride has been recorded

## Notes for next session

- Mock mode can be developed and tested *without* a real bike — useful for getting a working pipeline before touching hardware
- Test the WS contract by pointing the engine repo's `effort_bridge.gd` at the mock server early

## Entry point for next session

> "Bootstrap the repo (`git init` + push), then implement mock mode: Pydantic event models + WebSocket server + slider UI, all in Python."
