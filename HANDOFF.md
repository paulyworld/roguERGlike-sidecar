# HANDOFF — roguERGlike-sidecar

> Current state of this repo. Updated at the end of every session that touches it. Read first.

**Last updated:** 2026-05-20
**Last session log:** `../../docs/sessions/2026-05-20-bike-integration-handshake.md` (in umbrella)
**Current branch:** `feat/mock-mode` (PR #1) + `fix/replay-session-state` (PR #2, stacked) + `docs/handoff-refresh` (PR pending)
**Current focus:** Phase 1 mock mode complete and validated end-to-end against the engine; awaiting PR review before starting Phase 2 (real BLE).

## Where we are

Phase 1 ships a self-contained mock event source: Pydantic v2 event models match `docs/event-schema.md`, a `websockets` broadcaster on `localhost:8421` fans monotonic-seq envelopes out to every connected client, a 1 Hz mock producer turns slider state into power/cadence/heart_rate events, and an aiohttp slider page on `localhost:8422` mutates that state. The CLI `roguerglike-sidecar --mode mock` runs all four in one event loop. 13/13 unit tests pass; ruff and mypy clean; `scripts/e2e_smoke.py` exercises the full WS-plus-control round-trip; the engine's `test_main` scene confirms typed signals fire with the slider-driven values.

A follow-up fix (`fix/replay-session-state`, stacked on `feat/mock-mode`) closes the late-subscriber bug: clients that connect after `session_start` / `device_connected` were emitted now receive those events on connect.

## What's next (immediate)

1. **Merge PRs.** PR #1 (mock mode) → develop, then PR #2 auto-retargets and can merge.
2. **Phase 2 — real BLE.** New `feat/ble-ftms` branch: Bleak-based scanner, FTMS bike characteristic decoder, normalization into the same `EventBus`. `--mode live` selects it; mock stays.
3. **Session recording.** Append-only JSONL writer subscribed to `EventBus`. Foundation for replay mode and FIT export.

## Open threads

- **Derived signals** (`effort_surge_*`, `hr_zone_changed`, `effort_pulse`, `tempo_steady`) — schema defined, broadcaster ready, no producer yet. Defer until real telemetry exists to derive from.
- **CI matrix on Windows/macOS** runs on PRs; first runs hit on these PRs. Watch for OS-specific surprises (filesystem case, asyncio loop policy).
- **Leftover sidecar processes** can hold port 8421/8422 if killed roughly during local dev. Use `Get-NetTCPConnection -LocalPort 8421` + `Stop-Process` if a fresh start refuses to bind.
- **`websockets` API**: we use the new `websockets.asyncio.server` (handler is single-arg `async def(connection)`). Pin `>=12` for now; bump to `>=13` once we're sure all hosts have it.

## Notes for next session

- The event schema (`docs/event-schema.md`) is a stable contract. Breaking changes require a major version bump and coordinated update in `repos/engine`.
- Run the sidecar and the engine together to validate any change to the wire format (the `scripts/e2e_smoke.py` script is the in-repo equivalent without needing Godot).
- Pre-commit hook will rewrite ruff-CLI formatting differences — when commit aborts with "files were modified by this hook", just re-stage and re-commit.

## Entry point for next session

> "After PR #1 + #2 merge, start Phase 2 on a `feat/ble-ftms` branch: implement a Bleak scanner that pairs with FTMS bike trainers and decodes the Indoor Bike Data characteristic into the same `EventBus.publish` calls the mock producer uses. Select with `--mode live`; keep `--mode mock` working."
