"""FTMS Indoor Bike Data decoder.

Implements the *Indoor Bike Data* characteristic (``0x2AD2``) under the
*Fitness Machine Service* (``0x1826``) as defined in the Bluetooth SIG FTMS
spec v1.0. Pure decoder — no Bleak, no I/O.

Packet layout::

    [flags: u16 LE] [fields in fixed order, each present iff its flag bit is set]

The flags bit map and the field order are both defined by the spec; we read
the 2-byte flags field, then walk through the fields in spec order, advancing
the offset for each one that is present. Fields the schema does not yet
model (average speed, average power, resistance, energy, MET, elapsed /
remaining time) are still *consumed* (their bytes are stepped over) so the
following fields decode correctly.

Bit 0 of the flags field is the inverted *More Data* bit: when clear, the
instantaneous speed field IS present at the start of the data. When set, the
speed field is omitted (the device couldn't fit everything in one MTU).
"""

from __future__ import annotations

import struct
from collections.abc import Iterable

from ..events import (
    CadenceData,
    DeviceKind,
    DistanceData,
    EventData,
    EventType,
    HeartRateData,
    PowerData,
    SpeedData,
)

# Service / characteristic UUIDs (16-bit assigned numbers expanded to the
# full Bluetooth base UUID).
FTMS_SERVICE_UUID = "00001826-0000-1000-8000-00805f9b34fb"
INDOOR_BIKE_DATA_CHAR_UUID = "00002ad2-0000-1000-8000-00805f9b34fb"
# Fitness Machine Feature characteristic — 8 bytes total. Lower 4 bytes are
# the *sensor* feature bitmap (what the device measures); upper 4 bytes are
# the *target setting* feature bitmap (what control opcodes the device
# accepts on the Fitness Machine Control Point). Read once at connect.
FITNESS_MACHINE_FEATURE_CHAR_UUID = "00002acc-0000-1000-8000-00805f9b34fb"

# Target Setting Features bit positions (Bluetooth SIG FTMS v1.0 §4.3.2).
# Each bit set in the upper 4 bytes means the named control is supported.
_TS_BIT_POWER_TARGET = 1 << 3
_TS_BIT_RESISTANCE_TARGET = 1 << 2
_TS_BIT_INCLINATION_TARGET = 1 << 1
_TS_BIT_HEART_RATE_TARGET = 1 << 4
_TS_BIT_INDOOR_BIKE_SIM = 1 << 13


def parse_target_setting_features(features_payload: bytes) -> dict[str, bool]:
    """Decode the 8-byte FTMS Fitness Machine Feature characteristic into the
    subset of target-setting flags our schema models.

    Returns all-``False`` for short, empty, or malformed payloads — same
    "total" stance as the data decoder. Unknown reserved bits are ignored.
    """
    out = {
        "target_power": False,
        "target_resistance": False,
        "target_inclination": False,
        "target_heart_rate": False,
        "indoor_bike_simulation": False,
    }
    if len(features_payload) < 8:
        return out
    target_bits = struct.unpack_from("<I", features_payload, 4)[0]
    out["target_power"] = bool(target_bits & _TS_BIT_POWER_TARGET)
    out["target_resistance"] = bool(target_bits & _TS_BIT_RESISTANCE_TARGET)
    out["target_inclination"] = bool(target_bits & _TS_BIT_INCLINATION_TARGET)
    out["target_heart_rate"] = bool(target_bits & _TS_BIT_HEART_RATE_TARGET)
    out["indoor_bike_simulation"] = bool(target_bits & _TS_BIT_INDOOR_BIKE_SIM)
    return out


class FtmsBikeProfile:
    """BLE profile for FTMS-compatible bike trainers."""

    name: str = "FTMS Bike"
    service_uuid: str = FTMS_SERVICE_UUID
    char_uuid: str = INDOOR_BIKE_DATA_CHAR_UUID
    device_kind: DeviceKind = "bike_trainer"
    # Optional capability-discovery hook. Read by ``BleSource`` once at
    # connect; ``parse_features`` interprets the bytes. Profiles without a
    # feature characteristic leave this as ``None``.
    feature_char_uuid: str | None = FITNESS_MACHINE_FEATURE_CHAR_UUID

    def parse_features(self, payload: bytes) -> dict[str, bool]:
        """Map the FTMS Feature characteristic to capability booleans the
        schema's ``DeviceCapabilitiesData`` understands."""
        return parse_target_setting_features(payload)

    def decode(self, payload: bytes) -> Iterable[tuple[EventType, EventData]]:
        if len(payload) < 2:
            return
        flags = struct.unpack_from("<H", payload, 0)[0]
        offset = 2
        events: list[tuple[EventType, EventData]] = []

        # bit 0 INVERTED: 0 = instantaneous speed present, 1 = omitted.
        if not (flags & 0x0001) and offset + 2 <= len(payload):
            raw = struct.unpack_from("<H", payload, offset)[0]
            offset += 2
            events.append(("speed", SpeedData(kph=round(raw / 100.0, 2))))

        # bit 1: average speed (u16, 0.01 kph). Consumed; not yet in schema.
        if flags & 0x0002 and offset + 2 <= len(payload):
            offset += 2

        # bit 2: instantaneous cadence (u16, 0.5 rpm).
        if flags & 0x0004 and offset + 2 <= len(payload):
            raw = struct.unpack_from("<H", payload, offset)[0]
            offset += 2
            events.append(("cadence", CadenceData(rpm=raw // 2)))

        # bit 3: average cadence (u16, 0.5 rpm). Consumed.
        if flags & 0x0008 and offset + 2 <= len(payload):
            offset += 2

        # bit 4: total distance (u24 LE, m). Emitted as a ``distance`` event
        # with ``source="trainer"`` and ``meters_delta=0`` — the producer
        # (BleSource) tracks last_total to fill in the real delta before
        # publishing, since the decoder is stateless and shared across
        # potential reconnect cycles.
        if flags & 0x0010 and offset + 3 <= len(payload):
            raw = payload[offset] | (payload[offset + 1] << 8) | (payload[offset + 2] << 16)
            offset += 3
            events.append(
                (
                    "distance",
                    DistanceData(
                        meters_total=float(raw),
                        meters_delta=0.0,
                        source="trainer",
                    ),
                )
            )

        # bit 5: resistance level (s16, 0.1). Consumed.
        if flags & 0x0020 and offset + 2 <= len(payload):
            offset += 2

        # bit 6: instantaneous power (s16, watts).
        if flags & 0x0040 and offset + 2 <= len(payload):
            raw_s = struct.unpack_from("<h", payload, offset)[0]
            offset += 2
            # Schema bounds power to [0, 3000]; clamp negatives (regen brakes,
            # noise) to 0 rather than dropping the event.
            events.append(("power", PowerData(watts=max(0, min(raw_s, 3000)))))

        # bit 7: average power (s16, watts). Consumed.
        if flags & 0x0080 and offset + 2 <= len(payload):
            offset += 2

        # bit 8: expended energy (u16 total kcal, u16 kcal/h, u8 kcal/min). 5 B.
        if flags & 0x0100 and offset + 5 <= len(payload):
            offset += 5

        # bit 9: heart rate (u8, bpm).
        if flags & 0x0200 and offset + 1 <= len(payload):
            bpm = payload[offset]
            offset += 1
            # Schema requires bpm >= 20; FTMS uses 0 to mean "no reading".
            if 20 <= bpm <= 240:
                events.append(("heart_rate", HeartRateData(bpm=bpm)))

        # bit 10: metabolic equivalent (u8, 0.1). bits 11/12: elapsed/remaining
        # time (u16 each). All consumed only — not modelled yet.

        yield from events
