# Safety vs. Pause — two separate concerns

## TL;DR

**Cadence bailout is a safety mechanism, not a game-flow mechanism.**
Game-driven pauses (turn transitions, menu interactions, card-selection
phases) must NOT lean on the cadence bailout to drop resistance. They
need an explicit `pause` command from the engine.

## Why these are different

The cadence bailout drops the trainer target to the floor when the
rider has stopped pedaling for too long. It exists to answer one
question: **"the rider has walked away — should the trainer keep
applying resistance?"** Answer: no, because a high-watt target on an
unattended trainer is a hazard (knee, ankle, foot caught in pedals,
flywheel still spinning).

A game-driven pause asks a different question: **"the game is in a
phase where the rider isn't expected to be pedaling — should we ease
resistance temporarily?"** This has nothing to do with safety. It's a
UX concern about not making the rider grind against ERG while reading
card text.

Conflating these costs us in both directions:

- **Safety degrades.** The bailout window has to be tuned long enough
  not to trip during normal between-round UI moments, which means a
  rider who actually walked away gets resistance for that whole window.
  In a 250W FTP / 70% pct_ftp ride, that's ~75 seconds — fine if it's
  just a phase transition, much too long if the rider's foot is stuck.
- **UX degrades.** Riders who decide to spin during a between-round UI
  moment (which is a perfectly natural thing to do) reset the bailout
  timer, so the bailout never fires when they actually need it. Or
  they don't spin, get a sudden drop to 0W, then a 3.6s ramp back up
  when the next phase starts — which feels like a bug.

## How to apply this principle

### Engine side (eventually — not yet implemented)

The engine will send an explicit `pause` command at phase boundaries
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
3. Publishes a typed `paused` / `resumed` envelope so any other
   subscriber (recordings, dashboards) sees the phase change.

### Sidecar side (current state)

The current cadence bailout is correct for what it claims to do: a
last-resort safety net. Tuning its constants further is the wrong
move — making it shorter to "feel snappier" sacrifices safety to
paper over the missing pause command.

`CadenceBailout` already has `_paused` plumbing (used internally by
the resume-ramp), but no public command surface. When the engine
ships pause/resume, the sidecar's contribution is:

- A new `PauseCommand` / `ResumeCommand` discriminated-union pair in
  `events.py`.
- A handler in `cli.py` (live mode) and `mock.py` (mock mode) that
  toggles `CadenceBailout.set_paused(...)` and optionally sets a
  configurable pause-target wattage.
- Typed `paused` / `resumed` envelopes on the event bus.

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
"shorten the timer." It's "is the rider stopping for game reasons or
for life reasons?" The first needs the pause command. The second
needs the timer as it is.

## Evidence from the first live validation (2026-05-22)

In the first end-to-end ride post-bailout-fix, three bailouts fired
in ~10 minutes, all between game phases. The math was correct
(74-78s at ~177W with FTP 250 ≈ pct_ftp 0.71 → expected 74.4s). The
behavior was correct given the inputs. But the rider experience was:
"the trainer kept feeling heavy during card selection, then dropped
to zero, then ramped back to 164W when I started pedaling again."

That is exactly the symptom the pause command will fix. The bailout
is doing its job — it's just being asked the wrong question.

## Related

- `[[intensity-aware-safety-curves]]` — the curve design
- `[[trainer-control-aggressive-defaults]]` — permissive ERG defaults,
  safety as opt-in
- `[[opt-in-flags-need-feedback]]` — typed-event rejection pattern
  (pause/resume commands should follow the same shape)
