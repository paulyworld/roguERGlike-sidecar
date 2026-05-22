# HANDOFF — roguERGlike-sidecar

> Current state of this repo. Updated at the end of every session that touches it. Read first.

**Last updated:** 2026-05-21
**Last session log:** `../../docs/sessions/2026-05-21-cadence-bailout-paused-mid-validation.md` (in umbrella)
**Current branch:** `docs/post-cadence-bailout-paused-sidecar` (PR pending); develop has the cadence-bailout feature
**Current focus:** Cadence-based ERG bailout shipped (PR #14). **Three small bugs surfaced during live validation that block the next ride**; queued as one follow-up PR. Live ERG ride against KICKR + engine never got a clean run this session.

## Where we are

Trainer-control loop is now ostensibly complete on `develop`:

- **PR #14** merged (cadence-based ERG bailout + silent-rejection fix). Intensity-aware curve: `bailout_s` and `ramp_s` interpolate over `pct_ftp ∈ [0.5, 1.5]` per memory `intensity-aware-safety-curves`. New event types: `cadence_bailout_engaged` / `cadence_bailout_disengaged`. New CLI flags: `--rider-ftp`, `--cadence-bailout-s`, `--target-power-ramp-s`. Silent-failure footgun for `set_target_power` without `--allow-trainer-control` closed — sidecar now publishes a typed rejection envelope.
- All previous trainer-control work (PRs #9, #11, #12) on develop. Engine bridge (engine PR #9) on engine develop, mirroring the new event types.
- 105/105 unit tests pass; ruff + mypy clean.

**The live validation attempt failed** during this session — see open threads. Three real bugs need to land before the next attempt.

## What's next (immediate)

1. **`fix/bailout-startup-and-scan-ux`** — one PR, three small fixes (~45 min):
   - **Bug A — premature bailout.** `CadenceBailout._last_active_ts` is initialized at `__init__` time. The watcher's `idle_s = now - last_active_ts` therefore reflects "time since sidecar startup", not "time since rider stopped pedalling at a meaningful target". If the operator takes >60s between sidecar launch and the engine's first `set_target_power` (common — they have to launch Godot, configure rider settings, press Start), the watcher's first eligible tick sees `idle_s ≈ 60+, target=100W, pct_ftp=0.4` → engages bailout immediately, drops target to 0. Rider perceives ERG as broken. **Fix:** reset `_last_active_ts` whenever `handle_set_target_power` routes a non-floor target through. The window then correctly measures "time since last meaningful target / last active cadence" — which is the bailout's intended semantic.
   - **Bug B — fail-once scan.** `_run_live` calls `scan_for_profiles(timeout_s=8.0)` once. If any expected device isn't broadcasting in that 8s window (KICKR went to sleep, Whoop broadcast off), the sidecar prints `no FTMS bike matched 'KICKR'` and exits. The operator has to relaunch the whole command. Painful — every time. **Fix:** replace fail-once with a forgiving retry loop. Default total 30s, print every 5s what's still missing, succeed on full match, exit only if total elapses with anything still missing. Optional `--scan-timeout-s` flag.
   - **Bug C — no clear "control claimed" log line.** `BLE source FTMS Bike connected to KICKR CORE 1003` is emitted at GATT-connect time, BEFORE Request Control + Start. The actual moment when `set_target_power` becomes effective isn't explicitly logged — only inferable from absence of a `Request Control rejected` warning or from the bailout watcher arming. The KICKR's blue-solid LED is also misleading: it indicates BLE GATT connection only, not FTMS control claim. **Fix:** add `INFO ... claimed control of <device> — set_target_power now active` from `FtmsControl.request_control_and_start` after success.
2. **Live ERG retest** against KICKR (bike-only is fine; Whoop optional). Should pass after fixes A + B + C; if anything else surfaces, iterate.

Other candidates (not blocking):
- `--record <path>` as a first-class sidecar flag (replaces external `scripts/record_session.py` from docs PR #13). Small.
- FTMS SIM mode (slope/wind/CRR), distance deriver, CSCS profile — all carried from prior HANDOFFs.

## Open threads

- **Whoop broadcast UX is environmental, not project-side**. Whoop's "Broadcast Heart Rate" resets per-activity. To re-enable: the rider has to actively *Start an Activity* in the Whoop app on their phone. Toggling the broadcast setting alone is not enough on the user's current firmware. Worth a note in `docs/ble-profiles.md`'s HRS-compatibility section next time it's touched.
- **PowerShell paste is fragile** with long semicolon-separated commands and multiple flags — flags keep getting dropped on copy. For live tests, prefer a tiny `start_test.ps1` script with flags hard-coded.
- **KICKR LED is not a control-claim indicator** — solid blue means BLE GATT connection only. Bug C above gives operators a reliable log signal once it lands.
- **Bleak version pin** at `>=0.22.0`. Bleak's reconnect path inside `BleakClient._get_services` has been observed to terminate with `asyncio.CancelledError` after long-lived sessions (one observed during the prior session). Worth a chaos test eventually.
- **`fit-tool` runtime dep is unused** for now.
- **First CI runs** still subject to `github-actions-first-push-quirk` memory.

## Notes for next session

- Read `docs/event-schema.md` if anything touches the wire. The contract now covers events AND inbound commands AND the bailout subsection.
- Read `docs/ble-profiles.md` before touching device-side code; the HRS compatibility table and the HR de-duplication policy live there.
- KICKR + Whoop test setup: spin cranks to wake the trainer; *start an activity* in Whoop app to enable broadcast HR (toggle is not enough). Strap must have skin contact.
- Static fallback defaults: `--cadence-bailout-s 60`, `--target-power-ramp-s 3`. With `--rider-ftp 250` (or similar) the dynamic intensity-aware curve replaces these.

## Entry point for next session

> "Open `fix/bailout-startup-and-scan-ux` on develop. Three small fixes in one PR: (A) reset `_last_active_ts` in `CadenceBailout.handle_set_target_power` for non-floor targets; (B) replace `_run_live`'s fail-once scan with a 30s forgiving retry that prints per-5s 'still waiting for X'; (C) emit `INFO ... claimed control of <device> — set_target_power now active` from `FtmsControl.request_control_and_start` after success. Add tests for each. After merging, retry engine-driven ERG live validation against KICKR via the MVP scene's phase transitions."
