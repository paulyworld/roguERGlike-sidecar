# BLE profiles

A *profile* is the sidecar's unit of "BLE device type". It bundles three
things and nothing else:

- The GATT **service UUID** the device must advertise to count as a match.
- The **characteristic** to subscribe to via notifications.
- A pure **decoder** that turns notification payloads into our event-schema
  events.

A profile is data + a function. It has no I/O, no Bleak imports, and is
unit-testable against captured packet hex fixtures.

```python
class BleProfile(Protocol):
    name: str                       # "FTMS Bike", "HR Sensor"
    service_uuid: str               # full 128-bit UUID
    char_uuid: str
    device_kind: DeviceKind         # what device_kind events from this carry

    def decode(self, payload: bytes) -> Iterable[tuple[EventType, EventData]]:
        ...
```

## Why profiles

The pairing UX we're aiming for (Zwift-style: bike, HR strap, and controls
paired independently — see project memory `device-pairing-ux-model`) means
the sidecar will eventually run *several* BLE connections at once: a bike
trainer for power/cadence and a chest strap for HR, possibly more later
(rower, treadmill). Each is its own connection, its own profile, its own
decoder.

A `BleSource` wraps **one profile + one device address** and feeds the
shared `EventBus`. The CLI / future pairing UI composes whichever sources
are paired; the broadcaster doesn't care which.

## Adding a profile

Three pieces:

1. **Profile class** in `src/roguerglike_sidecar/ble/<name>.py`. Define the
   four attributes and `decode`. Keep it pure.
2. **Tests** in `tests/test_<name>.py` driving `decode` against handcrafted
   packet bytes for each flag combination + every malformed-input edge case
   you can think of (empty, truncated, unknown flags, out-of-range values).
   The FTMS bike tests are the reference template.
3. **CLI wiring** — add a `--device-<short>` flag, scan for the profile's
   service, resolve user input to a device, start a `BleSource(profile,
   address, name, bus)`. The shared `announce_session_start` call is fired
   once per process, not per source.

## Current profiles

| Profile | Module | Service | Characteristic | device_kind |
|---|---|---|---|---|
| FTMS Bike | `ble/ftms_bike.py` | `0x1826` Fitness Machine | `0x2AD2` Indoor Bike Data | `bike_trainer` |

## Planned

- **HR Sensor (HRS)** — service `0x180D`, characteristic `0x2A37` (Heart Rate
  Measurement). Standalone chest straps. When a bike already emits HR inside
  its FTMS packet, policy will be "prefer standalone HR sensor"; that
  dedup belongs above the profile (probably in a small `HrPolicy` selector)
  not inside either decoder.
- **Cycling Speed and Cadence (CSCS)** — service `0x1816`. Some older smart
  trainers and power meters expose cadence here rather than via FTMS.

## Design constraints

- **Decoders never raise on bad input.** They yield nothing, and the source
  carries on. A bad packet should never take the live stream down.
- **Decoders never block.** No I/O, no `await`. Synchronous, fast, pure.
- **Values out of schema range are clamped or dropped, not propagated raw.**
  The wire schema bounds (e.g. `PowerData.watts in [0, 3000]`) is a
  contract; the decoder is the place that enforces it. See the FTMS bike
  decoder's negative-power clamp and zero-HR drop for examples.
- **Unknown flag bits don't break alignment.** Always advance the read offset
  for fields whose flag is set, even if we don't yet model the event. The
  FTMS decoder consumes (without emitting) average speed / average power /
  energy / time fields for exactly this reason.
