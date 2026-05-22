# Screenshots — live hardware validation log

Visual evidence from validating sidecar features against real hardware. Each
shot pairs with a sentence or two below describing what it confirms.

## Naming convention

`YYYYMMDD_<short-slug>.<ext>` — date the validation ran, then a short slug
naming what's under test. Date-first so the directory sorts chronologically.

## Shots

### `20260521_ftms_write_test.jpg`

End-to-end live validation of FTMS Control Point writes (PR #10) against a
Wahoo KICKR CORE 1003. The sidecar was driven through a target ramp
`100 → 140 → 180 → 220 → 250 → 180 → 140 → 100 W` (10–12 s holds) via
`set_target_power` commands over the WS; every command came back accepted.

The screenshot is from the engine's MVP HIIT scene (`feat/mvp-playable-loop`)
running concurrently as a passive telemetry consumer — engine doesn't yet
have the write side (PR 3 / `feat/trainer-control-bridge` lands that). Key
things visible:

- **Power / Target Power chart** — the orange actual-power trace tracks the
  yellow target-dashes line cleanly through the entire ramp. The trainer is
  enforcing the commanded wattage in real time. This is the load-bearing
  evidence the FTMS write protocol works end-to-end against real hardware.
- **Cadence chart** — held around 100 rpm through the ramp. Classic ERG-mode
  rider behaviour (keep cadence, let the trainer change resistance).
- **HR chart** — flat zero. No chest strap was paired for this run; HR has
  been live-validated separately against a Whoop MG5 (PR #7 session).
- **Game logic** — MVP loop's `Phase: Power Interval` ran concurrently and
  reported `Prev power accuracy: 47%`, which is consistent with the rider
  hitting 100–250 W against the game's 300 W interval target. The game is
  robust to externally-driven target power that doesn't match its own phase
  goal.

The rider subjectively reported: "the ramp felt clean. the trainer
definitely got much harder in steps and then tapered back off."
