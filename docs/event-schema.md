# Event Schema

All events are newline-delimited JSON objects emitted over WebSocket. Stable contract — breaking changes require a major version bump.

## Design principles

1. **Device-neutral where possible.** `power` and `heart_rate` mean the same thing on a bike, a rower, or a treadmill. Game logic subscribes to these without knowing the source device.
2. **Device-specific events get their own type.** Cycling-only signals (`cadence`) and rowing-only signals (`stroke_rate`, `drag_factor`) are distinct event types, not overloads.
3. **Derived signals are normalized.** A "surge" is detected the same way whether it's a sprint on a bike or a power-10 on a rower — both surface as `effort_surge`.

## Envelope

Every event has:

```json
{
  "type": "<event_type>",
  "ts": 1715500000.123,
  "session_id": "uuid-v4",
  "seq": 12345,
  "device_kind": "bike_trainer",
  "data": { ... }
}
```

- `ts` — Unix timestamp, float seconds, monotonic within a session
- `seq` — strictly increasing sequence number, useful for ordering and replay
- `session_id` — stable for the life of a connection; new on each sidecar start
- `device_kind` — one of `bike_trainer`, `rower`, `treadmill`, `cross_trainer`, `power_meter`, `hr_sensor`, `mock`

## Universal events (all device types)

### `power`
Mechanical power output in watts.
```json
{ "type": "power", "data": { "watts": 247 } }
```

### `heart_rate`
```json
{ "type": "heart_rate", "data": { "bpm": 142 } }
```

### `distance`
Cumulative + delta distance with `source` attribution. The trainer-derived
flavor is emitted automatically when FTMS Indoor Bike Data carries the
total-distance field (bit 4 of the flags); the producer (`BleSource`)
tracks the prior cumulative to compute the real `meters_delta` before
publishing.
```json
{
  "type": "distance",
  "data": {
    "meters_total": 12450.3,
    "meters_delta": 2.1,
    "source": "trainer"
  }
}
```

`source` is one of:

| Source | Meaning |
|---|---|
| `trainer` | FTMS `meters_total` from the bike — real distance the wheel turned through |
| `synthetic` | Computed from a client-supplied terrain profile + trainer speed (ERG Terrain / SIM Terrain virtual distance) |
| `gps` | GPS / route-replay source |

**Do not conflate these in exports.** A 30 km ride that was a synthetic
concert ride is materially different from a 30 km outdoor route. The
FIT export currently includes whatever `distance` events the JSONL had;
analyzers should preserve the source distinction when feeding back
into platforms like Strava / TrainingPeaks. See memory
`terrain-distance-ownership` for the long-term policy.

### `elevation`
Cumulative + delta elevation gain with `source` attribution. Same
semantics as `distance` — `trainer` is rare (most bike trainers don't
report elevation); `synthetic` is the common case (computed from a
terrain profile + grade); `gps` is for route replay.
```json
{
  "type": "elevation",
  "data": {
    "meters_total": 245.0,
    "meters_delta": 1.2,
    "source": "synthetic"
  }
}
```

No sidecar-side producer emits `elevation` today — the event type exists
so terrain-aware clients can push their computed elevation samples into
the recording for FIT export. Future SIM-mode work will populate this
from FTMS Indoor Bike Simulation Parameters.

### `speed`
```json
{ "type": "speed", "data": { "kph": 32.4 } }
```

## Bike-specific events

### `cadence`
```json
{ "type": "cadence", "data": { "rpm": 88 } }
```

### `wheel_revolutions`
```json
{ "type": "wheel_revolutions", "data": { "total": 18234, "delta": 3 } }
```

## Rower-specific events (planned)

### `stroke_rate`
```json
{ "type": "stroke_rate", "data": { "spm": 28 } }
```

### `stroke`
Emitted on each stroke completion.
```json
{ "type": "stroke", "data": { "drive_time_s": 0.71, "recovery_time_s": 1.43, "stroke_distance_m": 8.4 } }
```

### `drag_factor`
Concept2-specific resistance metric.
```json
{ "type": "drag_factor", "data": { "value": 130 } }
```

## Treadmill-specific events (planned)

### `incline`
```json
{ "type": "incline", "data": { "percent": 4.5 } }
```

### `pace`
```json
{ "type": "pace", "data": { "seconds_per_km": 285 } }
```

## Derived signals (device-agnostic)

These are computed by the sidecar from raw streams. The game subscribes to these for high-level events rather than computing them itself. Same names work across all device types.

### `effort_surge_started` / `effort_surge_ended`
Detected when power exceeds 2× rolling 30s baseline for >2s. Bike sprint, rowing power-10, treadmill sprint — all surface here.
```json
{ "type": "effort_surge_started", "data": { "peak_watts": 612, "baseline_watts": 215 } }
```

### `tempo_steady`
Cadence (bike), stroke rate (rower), or pace (treadmill) held within a tight band for a configurable duration. The data field carries whichever rhythm metric applies to the device.
```json
{ "type": "tempo_steady", "data": { "metric": "cadence", "value": 90, "duration_s": 10 } }
```

### `hr_zone_changed`
Standard 5-zone model based on configured max HR or LTHR. Device-independent.
```json
{ "type": "hr_zone_changed", "data": { "from": 2, "to": 3, "bpm": 152 } }
```

### `effort_pulse`
Rolling 5s normalized power, emitted at 1Hz. Smoothed signal for game logic. Always in watts regardless of device.
```json
{ "type": "effort_pulse", "data": { "np_5s": 238, "watts_per_kg": 3.4 } }
```

## Session control

### `hello`
The sidecar's first envelope on every WS subscription. Self-describes the
protocol version, sidecar build, supported features, and mode. Replayed
to every new subscriber via the session-state mechanism — a client that
connects mid-session learns the contract immediately on attach.

```json
{
  "type": "hello",
  "ts": 1779560000.0,
  "session_id": "5d5590b6-c10e-4ed0-a3d9-1f8074caa69b",
  "seq": 0,
  "device_kind": "sidecar",
  "data": {
    "protocol_version": "1.0.0",
    "sidecar_version": "0.1.0",
    "features": ["set_target_power", "recording", "annotations"],
    "mode": "live"
  }
}
```

| Field | Meaning |
|---|---|
| `protocol_version` | Semver string. Minor bumps add features backward-compatibly; major bumps are breaking changes. Clients seeing an unknown major version should surface a visible warning. |
| `sidecar_version` | Sidecar build version (matches `roguerglike_sidecar.__version__`). Useful for bug reports; not contractual. |
| `features` | List of stable strings declaring sidecar protocol capabilities. Add a string when a new feature ships; never rename or remove without a major bump. |
| `mode` | `"mock"` / `"live"` / `"replay"`. Tells clients what data source is driving telemetry. |

`device_kind` is `"sidecar"`: hello isn't about a piece of hardware,
it's the sidecar describing itself. Distinct from `"client"` (used by
`rider_annotation` for rider-originated events).

**Recommended `features` vocabulary** (clients gate UI on these strings):

| Feature | Means the sidecar supports... |
|---|---|
| `set_target_power` | The `set_target_power` command (whether or not `--allow-trainer-control` is set; runtime gating surfaces via the `target_power_set` rejection ack) |
| `recording` | The `--record <path>` CLI flag for in-process JSONL recording |
| `annotations` | The `annotate` command + `rider_annotation` envelope |
| `structured_pause` | The `pause` / `resume` commands + `paused` / `resumed` envelopes |
| `distance` | `distance` + `elevation` events with `source` attribution (`trainer`/`synthetic`/`gps`) |
| `activity_export` | FIT export of completed sessions via the `roguerglike-export` CLI |
| `indoor_bike_simulation` | The `set_simulation` command — FTMS Set Indoor Bike Simulation Parameters writes (grade, wind, rolling resistance). Sidecar advertises the protocol capability; clients must also check `device_capabilities.indoor_bike_simulation` for the connected trainer. |

A feature in this list means *the sidecar will accept and respond to
the relevant commands*. It does **not** mean the connected hardware
supports it — that's `device_capabilities`. Clients gate trainer-write
UI on both: the protocol feature AND the device capability.

### `session_start` / `session_end`
Emitted by the sidecar at boundaries. `session_start` follows `hello`.

### `device_connected` / `device_disconnected`
```json
{ "type": "device_connected", "data": { "kind": "bike_trainer", "name": "KICKR CORE 8B2A" } }
```

### `device_capabilities`
Emitted once per device shortly after `device_connected`, when the profile
exposes a feature characteristic (FTMS reads `0x2ACC` for this). Flags are
pure capability indicators — they describe what the device *can* be asked
to do, not whether the host has asked or whether control is currently held.

For FTMS bikes the flags map to the Bluetooth SIG *Target Setting Features*
bitmap (upper 4 bytes of `0x2ACC`); profiles without a feature characteristic
(e.g. HRS heart-rate sensors) simply don't emit this event.

```json
{
  "type": "device_capabilities",
  "data": {
    "kind": "bike_trainer",
    "name": "KICKR CORE 8B2A",
    "target_power": true,
    "target_resistance": false,
    "target_inclination": false,
    "target_heart_rate": false,
    "indoor_bike_simulation": true
  }
}
```

Future trainer-control work (FTMS Control Point writes for ERG mode) is
gated on `target_power`; SIM-mode control is gated on `indoor_bike_simulation`.
Clients should treat unknown future flags as additive and not strict-validate.

## Trainer control

Trainer control (FTMS Control Point writes — ERG-mode target power) is
opt-in via the sidecar's `--allow-trainer-control` flag. When enabled, the
sidecar claims control of a connected FTMS bike at attach time (issues
Request Control + Start, awaits acknowledgement, then publishes
`control_acquired` so clients know commands will now have effect).

### `control_acquired`
```json
{ "type": "control_acquired", "data": { "kind": "bike_trainer", "name": "KICKR CORE 8B2A" } }
```

### `control_released`
Carries a `reason` field so the engine can distinguish graceful shutdowns
from safety bailouts (`ws_disconnect_bailout`, etc.).
```json
{ "type": "control_released", "data": { "kind": "bike_trainer", "name": "KICKR CORE 8B2A", "reason": "stop" } }
```

### `target_power_set`
Acknowledges (or rejects) a client `set_target_power` command. `watts` is
the value actually sent to the trainer after sidecar-side clamping; for a
rejected command it's the requested value with `accepted: false` and
`reason` populated. Known rejection reasons today:

- `"trainer-control disabled (no --allow-trainer-control flag)"` — sidecar
  is read-only; the engine should surface this to the rider rather than
  silently ignoring (per project memory `opt-in-flags-need-feedback`).
- `"no controllable bike"` — `--allow-trainer-control` was set but no FTMS
  bike has a live control session yet (e.g. connection in flight or lost).
- `"not controlling"` — Request Control / Start hasn't completed.
- `"bailout-pending"` — cadence bailout is engaged; the new target was
  saved as the *intended* restore value but won't be written to the
  trainer until cadence resumes.
- `"trainer rejected (0x##)"` — trainer's FTMS response indication carried
  a non-success result code; the hex code is the spec's result code byte.
- `"indication timeout"` — trainer never sent the response indication.

```json
{ "type": "target_power_set", "data": { "watts": 235, "accepted": true, "reason": "" } }
```

### `simulation_set`
Acknowledges (or rejects) a client `set_simulation` command. Echoed
fields reflect the values sent to the trainer. See the inbound
`set_simulation` command docs above for the full list of rejection
reasons. Same `accepted` / `reason` pattern as `target_power_set`.

```json
{
  "type": "simulation_set",
  "data": {
    "grade_percent": 4.5,
    "wind_speed_mps": 0.0,
    "rolling_resistance": 0.004,
    "wind_resistance": 0.51,
    "accepted": true,
    "reason": ""
  }
}
```

### `cadence_bailout_engaged`
The sidecar's cadence-bailout safety has dropped the trainer's target to
`--min-target-power` because cadence has been below the active threshold
(≈30 rpm) long enough that the rider is no longer pedalling. The engine
should treat this as a soft pause (UI overlay, optional clock pause).
The trainer is still under control; resume is automatic on cadence ≥ ~30
rpm. The wait duration is intensity-aware when `--rider-ftp` is set —
short bailout at high % FTP, longer at low (see project memory
`intensity-aware-safety-curves`).

```json
{
  "type": "cadence_bailout_engaged",
  "data": {
    "kind": "bike_trainer",
    "name": "KICKR CORE 8B2A",
    "pre_pause_target_watts": 300,
    "bailout_after_s": 22.5
  }
}
```

### `paused` / `resumed` (structured pause — Pattern B)

Client-driven pause / resume of the workout, distinct from the cadence-driven
safety bailout above. See `docs/architecture/safety-vs-pause.md` for the full
rationale; in short: cadence bailout is "rider walked away" (safety); structured
pause is "the workout/UI is paused" (flow). Both can coexist — but structured
pause suppresses cadence-bailout firing while it's active.

```json
{
  "type": "paused",
  "data": {
    "kind": "bike_trainer",
    "name": "KICKR CORE 8B2A",
    "reason": "between-rounds",
    "target_watts": 75,
    "previous_target_watts": 210
  }
}
```

```json
{
  "type": "resumed",
  "data": {
    "kind": "bike_trainer",
    "name": "KICKR CORE 8B2A",
    "restored_to_watts": 210,
    "ramped_over_s": 3.0
  }
}
```

| Field (`paused`) | Meaning |
|---|---|
| `reason` | Pass-through from the `pause` command (empty string if none supplied). For logs / post-ride analysis. |
| `target_watts` | The easy-spin wattage the trainer was set to. Result of: command override → CLI `--pause-easy-spin-pct-ftp × --rider-ftp` → CLI `--pause-easy-spin-w` fallback, clamped to trainer min/max. |
| `previous_target_watts` | Whatever the trainer was holding when the pause arrived — captured so resume knows what to restore to. If `set_target_power` is issued during the pause, this updates to the new value (the resume restores to the most recent intent). |

| Field (`resumed`) | Meaning |
|---|---|
| `restored_to_watts` | Final wattage at the end of the ramp |
| `ramped_over_s` | Ramp duration, intensity-aware per the same curve cadence-bailout uses |

While structured-paused, `set_target_power` commands are queued (not written
to the trainer) and acked with `target_power_set accepted=false
reason="deferred-paused"`. The queued value becomes the new restore target.

### `cadence_bailout_disengaged`
Cadence resumed; the sidecar has finished ramping the target back to the
pre-pause value (or whatever the engine set via `set_target_power` during
the pause, since those commands update the intended restore value).
```json
{
  "type": "cadence_bailout_disengaged",
  "data": {
    "kind": "bike_trainer",
    "name": "KICKR CORE 8B2A",
    "restored_to_watts": 300,
    "ramped_over_s": 7.0
  }
}
```

## Inbound commands (engine → sidecar)

The WS connection is bidirectional. Clients send commands using a separate
envelope shape (no `ts`/`seq`/`session_id` — the sidecar isn't logging
inbound traffic into the event stream). Malformed messages are logged and
dropped without affecting the outbound event flow.

### `set_target_power`
```json
{ "type": "set_target_power", "watts": 235 }
```
Sidecar clamps `watts` to its configured `[--min-target-power,
--max-target-power]` before writing the FTMS Set Target Power opcode. The
outbound `target_power_set` acknowledges with the post-clamp value.

### `start`
```json
{ "type": "start" }
```
Issues FTMS Start (opcode `0x07`) — enters active workout state. The
sidecar already does this automatically at connect when
`--allow-trainer-control` is set, so this command is mainly useful for
re-acquiring after a `release_control`.

### `stop`
```json
{ "type": "stop" }
```
Issues FTMS Stop (opcode `0x08`, stop subcode `0x01`). Releases the
controlling state; the trainer goes back to passive telemetry.

### `release_control`
```json
{ "type": "release_control" }
```
Same wire effect as `stop`, semantically distinct: the game is signaling
that this session is done using trainer control. The sidecar publishes
`control_released` with `reason: "requested"`.

### `set_simulation`
```json
{
  "type": "set_simulation",
  "grade_percent": 4.5,
  "wind_speed_mps": 0.0,
  "rolling_resistance": 0.004,
  "wind_resistance": 0.51
}
```
FTMS Set Indoor Bike Simulation Parameters (opcode `0x11`). The
trainer adjusts its physics model so resistance corresponds to riding
up/down the given grade with the given headwind / rolling resistance /
aerodynamic drag. Rider controls power by gear + cadence — inverse of
ERG mode.

Used by SIM Terrain mode (gizzERG) and future route-replay modes.

| Field | Required | Notes |
|---|---|---|
| `grade_percent` | yes | ±100% (FTMS encodes ±327.67%, capped here at the physically plausible range) |
| `wind_speed_mps` | no | Default 0.0. Positive = headwind. FTMS range ±32.767 m/s. |
| `rolling_resistance` | no | Default 0.004 (typical road tires per Zwift/TrainerRoad). FTMS range [0, 0.0255]. |
| `wind_resistance` | no | Default 0.51 (typical road position per Zwift/TrainerRoad). FTMS range [0, 2.55]. |

**Gated on three things** — clients should check all before showing
SIM-mode UI:

1. Sidecar startup with `--allow-trainer-control`
2. `indoor_bike_simulation` in the `hello` envelope's features
3. `device_capabilities.indoor_bike_simulation` for the connected trainer

If any are missing, the sidecar publishes `simulation_set
accepted=false` with a typed `reason` so the client can distinguish:

- `"no controllable bike"` — no FTMS bike attached
- `"not controlling"` — trainer control not claimed yet
- `"trainer rejected (0xNN)"` — trainer responded with a non-success code
  (e.g. `0x05` = Op Code Not Supported, despite the feature flag claim)
- `"indication timeout"` / `"ble error: ..."` — wire-layer failure

The successful ack (`simulation_set accepted=true`) echoes back the
exact values sent to the trainer.

Switching between SIM and ERG mid-ride: just stop sending one and start
sending the other. The trainer holds whatever mode it was most recently
asked for. No explicit "switch mode" command.

### `pause` / `resume` (structured pause — Pattern B)
```json
{ "type": "pause", "reason": "between-rounds", "target_watts": 75 }
{ "type": "resume" }
```

Client-driven workout pause. The sidecar drops the trainer target to the
easy-spin wattage (`target_watts` if supplied, else CLI default), suspends
the cadence-bailout watcher, and queues subsequent `set_target_power`
writes as "deferred-paused" acks. `resume` ramps back to the pre-pause
target (or the latest pending target supplied via `set_target_power`
during the pause).

| Field | Required | Notes |
|---|---|---|
| `reason` | no | Short descriptive tag (≤64 chars) for logs / events / post-ride analysis. Free-form; passes through unchanged on the `paused` envelope. |
| `target_watts` | no | Explicit easy-spin override. If omitted, sidecar uses `--pause-easy-spin-pct-ftp × --rider-ftp` (when FTP is known) or `--pause-easy-spin-w` (when not). Clamped to trainer min/max before write. |

Both commands are **idempotent**: a `pause` when already paused only
updates the easy-spin (if `target_watts` is supplied); no second
`paused` envelope fires. A `resume` when not paused is a silent no-op.

Distinct from cadence-bailout safety pause: see
`docs/architecture/safety-vs-pause.md`. The structured pause does NOT
auto-resume on rider pedalling — only an explicit `resume` clears it.

### `annotate`
```json
{
  "type": "annotate",
  "tag": "too-hard",
  "note": "ramp came back too hot after the pause",
  "client_id": "gizzERG",
  "client_time_s": 2412.5,
  "context": {
    "profile_id": "king-gizzard-night-2",
    "profile_version": "terrain-v0",
    "mode": "terrain_erg",
    "section": "Motor Spirit",
    "target_watts": 228,
    "power": 205,
    "cadence": 71,
    "hr": 154,
    "wkg": 2.93,
    "grade": 5.5,
    "hardware_source": "trainer_power"
  }
}
```
Mark a moment on the event stream. Used by clients to capture rider
intent or context that isn't otherwise visible in telemetry — F2-keypress
tuning feedback ("too hard", "too easy", "bad sync"), bug reports,
manual phase boundaries during debugging. The sidecar republishes the
payload as a `rider_annotation` envelope (see below) with its own `ts`
and `seq` so timestamps stay monotonic with the rest of the stream.

**Not gated on `--allow-trainer-control`.** Annotations never write to
the trainer, so the sidecar accepts them in any launch configuration —
including off-bike mock mode and live mode without trainer control.

#### Fields

| Field | Required | Type | Notes |
|---|---|---|---|
| `tag` | yes | string (1–64) | Short categorical label; free-form (see recommended vocabulary below) |
| `note` | no | string (≤280) | Free-text rider note. Omitted from the wire when unset. |
| `client_id` | no | string (≤64) | Which client UI sourced the annotation (e.g. `gizzERG`, `engine`). Omitted when unset. |
| `client_time_s` | no | float (≥0) | Rider's video/workout position at the keypress. Distinct from the sidecar wall-clock `ts` — lets analyzers place markers on the ride timeline rather than the receipt timeline. Omitted when unset. |
| `context` | no | object | Free-form pass-through blob; sidecar treats as opaque. See recommended shape below. Omitted when unset. |

Tags are intentionally not enum-constrained on the wire — different
clients can converge on a shared vocabulary without the sidecar
gatekeeping.

#### Recommended tag vocabulary

| Tag | Meaning |
|---|---|
| `too-hard` | Section felt subjectively too hard for the rider |
| `too-easy` | Section felt subjectively too easy |
| `bad-sync` | Audio/video sync looks wrong (music behind/ahead of intensity profile) |
| `false-intensity` | Profile says hard but the music feels easy here |
| `missed-intensity` | Music feels hard but the profile is treating it as easy |
| `cadence-mismatch` | Target cadence doesn't match what the section wants |
| `ui-pause` | Client is pausing for its own reasons (between rounds, between videos) — distinct from cadence bailout / walk-away |
| `walk-away` | Rider actually stopped pedalling intentionally |
| `bug` | Something visibly broke; `note` should explain |
| `marker` | Generic timestamp marker for later analysis |

#### Recommended `context` shape

The sidecar doesn't validate inside `context`; the shape below is a
convention so clients and analyzers speak the same vocabulary. Include
whatever fields the client has at hand; omit the rest.

| Field | Meaning |
|---|---|
| `profile_id` | The ride profile being played (e.g. `king-gizzard-night-2`) |
| `profile_version` | Version of that profile so leaderboards/ghosts compare like with like |
| `mode` | Ride mode: `raw_feel`, `tempo_intervals`, `terrain_erg`, `terrain_sim`, etc. |
| `video_id` | Source media id (YouTube id, file path, ...) |
| `section` | Current song/section label |
| `target_watts` | ERG target wattage at the moment of annotation |
| `power`, `cadence`, `hr` | Most recent telemetry snapshot |
| `wkg` | Power in watts per kg |
| `grade` | Current virtual grade percent (terrain modes) |
| `speed_kph` | Virtual or trainer-reported speed |
| `distance_m`, `elevation_gain_m` | Accumulated distance/elevation for the ride |
| `hardware_source` | `trainer_power` / `power_meter` / `estimated_power` / `mock` |

## Client-originated events (annotations)

### `rider_annotation`
The sidecar's republishing of an inbound `annotate` command. Sidecar
stamps `ts`/`seq`/`session_id` so the annotation is consistent with the
rest of the stream and lands correctly in JSONL recordings. Payload
fields and recommended vocabulary mirror the `annotate` command above
exactly — sidecar copies the rider-supplied fields through unchanged.
```json
{
  "type": "rider_annotation",
  "ts": 1779560002.114,
  "session_id": "5d5590b6-c10e-4ed0-a3d9-1f8074caa69b",
  "seq": 508,
  "device_kind": "client",
  "data": {
    "tag": "too-hard",
    "client_id": "gizzERG",
    "client_time_s": 2412.5,
    "context": {
      "mode": "terrain_erg",
      "grade": 5.5,
      "section": "Motor Spirit",
      "target_watts": 228
    }
  }
}
```
Optional fields the rider didn't fill in are omitted from the wire
(serialized with `exclude_none=true`), not present as explicit `null`s.
The `device_kind` field is `"client"` rather than a hardware kind:
annotations don't describe a device. This is the only event type that
uses `"client"` today.
