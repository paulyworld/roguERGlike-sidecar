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
Virtual or actual distance covered.
```json
{ "type": "distance", "data": { "meters_total": 12450.3, "meters_delta": 2.1 } }
```

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

### `session_start` / `session_end`
Emitted by the sidecar at boundaries.

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
