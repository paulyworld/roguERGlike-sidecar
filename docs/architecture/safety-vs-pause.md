# Safety vs. Pause — two separate concerns

## TL;DR

**Cadence bailout is a safety mechanism, not a game-flow mechanism.**
Game-driven pauses (turn transitions, menu interactions, video paused
on YouTube) must NOT lean on the cadence bailout to drop resistance.
They need their own pause primitive, owned by the client.

Two valid implementations of that primitive exist — pick by client
context (see "Two patterns" below).

## Why these are different

The cadence bailout drops the trainer target to the floor when the
rider has stopped pedaling for too long. It exists to answer one
question: **"the rider has walked away — should the trainer keep
applying resistance?"** Answer: no, because a high-watt target on an
unattended trainer is a hazard (knee, ankle, foot caught in pedals,
flywheel still spinning).

A client-driven pause asks a different question: **"my workout clock
is paused (video paused, game in a menu, between rounds) — should we
ease resistance temporarily?"** This has nothing to do with safety.
It's a UX concern about not making the rider grind against ERG while
reading card text or skipping ahead in a video.

Conflating these costs us in both directions:

- **Safety degrades.** The bailout window has to be tuned long enough
  not to trip during normal client UI moments, which means a rider who
  actually walked away gets resistance for that whole window. In a
  250W FTP / 70% pct_ftp ride, that's ~75 seconds — fine if it's just
  a phase transition, much too long if the rider's foot is stuck.
- **UX degrades.** Riders who decide to spin during a pause (perfectly
  natural) reset the bailout timer, so the bailout never fires when
  they actually need it. Or they don't spin, get a sudden drop to 0W,
  then a 3.6s ramp back up when playback resumes — which feels like a
  bug.

## Two patterns for the pause primitive

There is no single right answer. The pause trigger differs by client,
so the pause implementation can too.

### Pattern A — Client-side soft pause

**Used by:** `repos/concert-mvp` (shipped 2026-05-22)

The client sends a low `set_target_power` (e.g. 45% FTP) the moment
its own pause condition fires. The sidecar's cadence bailout watcher
keeps running, unchanged. Zero new sidecar contract.

The overlap case — client soft-pause target landing during an active
bailout — is handled by the sidecar's existing `bailout-pending`
rejection reason. The client surfaces this in UI as "queued by
cadence bailout" rather than treating it as a hard failure.

**Pros:**
- No sidecar changes needed; reuses existing primitives.
- Each client owns its own pause semantics (different soft-pause
  wattage per client, different cadence guidance, different UX copy).
- Simpler to reason about for ad-hoc, user-initiated pauses.

**Cons:**
- The bailout watcher is still ticking during a soft pause. A long
  soft pause at low intensity (e.g. video paused for 90s on a 100W
  section) will trip the bailout anyway, and the client has to handle
  the `bailout-pending` reply gracefully when it tries to nudge the
  target afterwards.
- Each client re-implements its own pause logic.

**Best for:** clients with user-initiated, unpredictable pause
triggers — browser apps that respond to YouTube play/pause events, ad
hoc "spin down" buttons, anything where the pause boundary is not
known in advance to the engine.

### Pattern B — Sidecar-side suspend

**Used by:** none yet; planned for `repos/engine-mvp` (Godot
turn-based MVP) where phase transitions are structural.

The client sends an explicit `pause` command at phase boundaries
where the rider is not expected to be pedaling:

```json
{ "command": "pause", "reason": "phase_transition" }
```

And a `resume` command when the phase ends:

```json
{ "command": "resume" }
```

While paused, the sidecar:

1. Suspends the cadence bailout watcher — the "no cadence" timer
   does not advance, so a long pause does not trigger a bailout.
2. Optionally drops the target to a configurable easy-spin wattage
   (e.g. 50W) — distinct from the bailout's 0W floor. Riders can
   choose to spin or rest; either is fine.
3. Publishes typed `paused` / `resumed` envelopes so other
   subscribers (recordings, dashboards) see the phase change.

**Pros:**
- Bailout and pause are cleanly separated; the watcher is genuinely
  off during structured pauses.
- Centralizes pause behavior in the sidecar — multiple clients sharing
  the same trainer get the same easy-spin policy.
- Typed envelope means recordings/replays know the difference between
  "rider stopped" and "game paused."

**Cons:**
- New command + new envelopes — schema change requires a coordinated
  bump and a client+sidecar version match.
- Each client wanting custom easy-spin behavior has to either accept
  the sidecar default or pass per-pause params.

**Best for:** clients with structured, code-driven pause boundaries —
turn-based games, training apps with explicit interval rests, anywhere
the pause is a well-defined phase, not a user gesture.

## How to apply this principle

### When designing a new client

Ask: **"is my pause a well-known phase boundary in code, or a user
gesture?"**

- User gesture → Pattern A. Send a soft target, handle
  `bailout-pending` gracefully.
- Code phase → Pattern B (once shipped). Send `pause`/`resume`, let
  the sidecar manage easy-spin and watcher state.

### When tuning the bailout curve

**Don't.** If the bailout is firing on legitimate phase transitions,
the right answer is the pause primitive (whichever pattern fits the
client), not a shorter window. Shortening the curve to feel snappier
in normal UI sacrifices safety to paper over the missing pause.

### When the sidecar-side suspend ships

Sidecar contributions are small and well-scoped:

- New `PauseCommand` / `ResumeCommand` discriminated-union pair in
  `events.py`.
- A handler in `cli.py` (live mode) and `mock.py` (mock mode) that
  toggles `CadenceBailout.set_paused(...)` and optionally sets a
  configurable pause-target wattage.
- Typed `paused` / `resumed` envelopes on the event bus.

`CadenceBailout` already has internal `_paused` plumbing (used for
the resume ramp) — the new command path wires into the existing
state, doesn't fork from it.

## What this means for safety-curve tuning

The intensity-aware bailout curve
(`PCT_FTP_LOW=0.5 → 90s`, `PCT_FTP_HIGH=1.5 → 15s`) was tuned with
this principle in mind: **assume the rider is genuinely pedaling
until proven otherwise.** It is intentionally patient at low intensity
because at 100W on a recovery spin, a 30s pause to grab water is
normal. It is intentionally aggressive at high intensity because at
350W during a VO2max interval, 15s of zero cadence is a
crash-or-bailed-off-the-bike signal.

If the curve feels wrong during validation, the question is **not**
"shorten the timer." It's "is the rider stopping for client reasons or
for life reasons?" The first needs the pause primitive. The second
needs the timer as it is.

## Evidence from the first live validation (2026-05-22)

In the first end-to-end ride post-bailout-fix, three bailouts fired
in ~10 minutes, all between game phases. The math was correct
(74-78s at ~177W with FTP 250 ≈ pct_ftp 0.71 → expected 74.4s). The
behavior was correct given the inputs. But the rider experience was:
"the trainer kept feeling heavy during card selection, then dropped
to zero, then ramped back to 164W when I started pedaling again."

That is exactly the symptom the pause primitive fixes. The bailout
was doing its job — it was just being asked the wrong question.

Concert-mvp shipped Pattern A shortly afterwards and confirmed the
principle works without sidecar changes. Engine-mvp's Pattern B
implementation remains future work.

## Related

- `[[intensity-aware-safety-curves]]` — the curve design
- `[[trainer-control-aggressive-defaults]]` — permissive ERG defaults,
  safety as opt-in
- `[[opt-in-flags-need-feedback]]` — typed-event rejection pattern
  (Pattern B's `pause`/`resume` commands should follow the same shape)
