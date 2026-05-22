"""BLE Heart Rate Service decoder.

Implements the *Heart Rate Measurement* characteristic (``0x2A37``) under the
*Heart Rate Service* (``0x180D``) per Bluetooth SIG spec v1.0. Pure decoder —
no Bleak, no I/O.

The HRS profile is the broad-ecosystem path: most consumer HR-broadcasting
devices already speak this protocol (Polar / Wahoo / Garmin chest straps,
COROS, Suunto, smartwatches with broadcast apps, Whoop 4+ in broadcast mode).
Vendor-specific protocols (Whoop native, Oura, ANT+-only devices) are out of
scope here — they belong as additional profiles if/when added later.

Packet layout::

    [flags: u8] [hr value: u8 or u16 LE] [energy expended: u16 LE]? [rr intervals: u16 LE...]?

Flags byte:

- bit 0   HR value is u16 (instead of u8)
- bits 1-2 Sensor contact status (0/1 = unsupported, 2 = no contact, 3 = contact)
- bit 3   Energy expended present (u16, kJ, follows HR)
- bit 4   RR-interval(s) present (one or more u16, 1/1024 s, fill remainder)
- bits 5-7 Reserved
"""

from __future__ import annotations

import struct
from collections.abc import Iterable

from ..events import DeviceKind, EventData, EventType, HeartRateData

HRS_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT_CHAR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"


class HrsProfile:
    """BLE profile for any device that broadcasts as a Heart Rate Sensor."""

    name: str = "HR Sensor"
    service_uuid: str = HRS_SERVICE_UUID
    char_uuid: str = HR_MEASUREMENT_CHAR_UUID
    device_kind: DeviceKind = "hr_sensor"
    # HRS has no relevant feature characteristic for our schema (the Body
    # Sensor Location char exists but doesn't map to any of our capability
    # flags). Leave the optional capability hook off.
    feature_char_uuid: str | None = None

    def decode(self, payload: bytes) -> Iterable[tuple[EventType, EventData]]:
        if len(payload) < 2:
            # need at minimum: 1 byte flags + 1 byte HR
            return
        flags = payload[0]
        hr_is_u16 = bool(flags & 0x01)
        # Sensor-contact bits (1-2) are informational; we don't gate on them
        # because some devices report "unsupported" even while reading correctly.
        energy_present = bool(flags & 0x08)
        # RR interval bytes are consumed by virtue of being at the end of the
        # packet; we don't have to step over them since nothing follows.

        offset = 1
        hr_size = 2 if hr_is_u16 else 1
        if offset + hr_size > len(payload):
            return
        bpm = struct.unpack_from("<H", payload, offset)[0] if hr_is_u16 else payload[offset]
        offset += hr_size

        if energy_present:
            # Skip 2 bytes if claimed-and-present, else skip the whole event
            # to keep alignment honest. The remaining RR-interval bytes fill
            # the rest of the packet and don't need explicit handling.
            if offset + 2 > len(payload):
                return
            offset += 2

        # Schema bounds bpm to [20, 240]; HRS uses 0 to indicate "no reading".
        # Out-of-range values (electrical noise, dropped contact) are dropped
        # rather than clamped — unlike FTMS power, a wildly-wrong HR value
        # would actively mislead the game.
        if 20 <= bpm <= 240:
            yield ("heart_rate", HeartRateData(bpm=bpm))
