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
| HR Sensor | `ble/hrs.py` | `0x180D` Heart Rate Service | `0x2A37` Heart Rate Measurement | `hr_sensor` |

### HR Sensor compatibility

The HR Sensor profile is the **broad-ecosystem** path — any device that
broadcasts as a BLE Heart Rate Sensor works without vendor-specific code.
That covers (representative sample, not exhaustive):

- **Out of the box:** Polar H10 / H9 / Verity Sense, Wahoo TICKR (all
  variants), Garmin HRM-Pro / HRM-Dual, COROS HR Monitor, Suunto Smart
  Sensor, most BLE-only chest straps from any vendor.
- **With a companion app re-broadcasting:** Apple Watch (via apps like
  HeartCast / BLE Heart Rate / HRV4Training), Wear OS smartwatches (similar
  apps), Garmin watches in broadcast mode.
- **In a specific mode:** Whoop 4.0+ (turn on "Broadcast HR" in the Whoop
  app). Older Whoop is proprietary-only.
- **Doesn't work via HRS (would need a vendor-specific profile, not in
  scope):** Oura rings (no real-time BLE HR — syncs to its app over
  proprietary services), ANT+-only devices.

When pitching the feature, frame it as *"any device that broadcasts as a
BLE Heart Rate Sensor"* — accurate, avoids vendor promises.

### HR de-duplication policy

Many FTMS bike trainers embed a heart rate field in their Indoor Bike Data
packet (from a strap paired to the trainer's head unit, or in some cases
from a wrist sensor on a fancier trainer). When the user also pairs a
standalone HR sensor through the sidecar, two sources are publishing
`heart_rate` events and the engine would see them interleaved.

Policy: **the standalone HR sensor wins by default.** The bike's FTMS
source is started with `drop_event_types={"heart_rate"}`, so its embedded
HR is silently discarded while bike-specific events (power, cadence, speed)
flow normally. The `--prefer-bike-hr` CLI flag inverts this — useful in the
unusual case where the rider trusts the bike's HR pickup more than chest
strap reception in their setup.

Implementation lives in `_resolve_drop_types` in `cli.py` (small pure
function, easy to unit-test). It returns the `(bike_drops, hr_drops)` pair
based on which devices are paired and the prefer-bike-hr flag.

## Planned

- **Cycling Speed and Cadence (CSCS)** — service `0x1816`. Some older smart
  trainers and power meters expose cadence here rather than via FTMS.
- **Stride Sensor (RSC)** — service `0x1814`. Treadmill / footpod pace
  + cadence. Out of scope until a treadmill game is in design.

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
