"""BleProfile protocol — what every BLE device type must provide.

A profile is pure data + a pure decode function. It has no I/O, no Bleak
dependency, and can be unit-tested against captured packet hex fixtures
without any Bluetooth adapter present. ``BleSource`` is responsible for the
actual connection lifecycle.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from ..events import DeviceKind, EventData, EventType


@runtime_checkable
class BleProfile(Protocol):
    """One BLE device type (e.g. FTMS bike trainer)."""

    #: Human-readable name for logging and the UI ("FTMS Bike").
    name: str

    #: GATT service the device must advertise to be considered a match.
    service_uuid: str

    #: Characteristic to subscribe to via notifications.
    char_uuid: str

    #: What ``device_kind`` events from this profile should carry.
    device_kind: DeviceKind

    def decode(self, payload: bytes) -> Iterable[tuple[EventType, EventData]]:
        """Turn one notification packet into zero or more event tuples.

        Implementations should be total: malformed or empty payloads yield
        nothing rather than raising. Any unsupported flags in the payload
        should be silently skipped so an unknown future device extension
        doesn't break the stream.
        """
        ...
