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


class FtmsBikeProfile:
    """BLE profile for FTMS-compatible bike trainers."""

    name: str = "FTMS Bike"
    service_uuid: str = FTMS_SERVICE_UUID
    char_uuid: str = INDOOR_BIKE_DATA_CHAR_UUID
    device_kind: DeviceKind = "bike_trainer"

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

        # bit 4: total distance (u24, m). Consumed; meaningful distance events
        # need a stateful deriver (meters_delta = total - previous_total) which
        # belongs outside the pure decoder. Tracked as a follow-up.
        if flags & 0x0010 and offset + 3 <= len(payload):
            offset += 3

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
