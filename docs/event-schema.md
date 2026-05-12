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
