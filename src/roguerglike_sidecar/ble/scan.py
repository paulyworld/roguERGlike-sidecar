"""Scan for BLE devices advertising a given service UUID.

Thin wrapper over ``BleakScanner.discover`` so the CLI / future pairing UI
can list candidate devices for one profile without standing up a full source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

DEFAULT_SCAN_TIMEOUT_S = 8.0


@dataclass(frozen=True)
class DiscoveredDevice:
    address: str
    name: str
    rssi: int | None


async def scan_for_service(
    service_uuid: str,
    *,
    timeout_s: float = DEFAULT_SCAN_TIMEOUT_S,
) -> list[DiscoveredDevice]:
    """Discover devices advertising ``service_uuid``.

    Returns a name-sorted list of matches. Devices that did not advertise a
    local name show up as ``"(unnamed)"`` so the address is still selectable.
    """
    from bleak import BleakScanner  # local import keeps unit tests Bleak-free

    log.info("scanning for %s for %.1fs", service_uuid, timeout_s)
    raw = await BleakScanner.discover(
        timeout=timeout_s,
        service_uuids=[service_uuid],
        return_adv=True,
    )
    out: list[DiscoveredDevice] = []
    for address, (device, adv) in raw.items():
        out.append(
            DiscoveredDevice(
                address=address,
                name=device.name or adv.local_name or "(unnamed)",
                rssi=adv.rssi,
            )
        )
    out.sort(key=lambda d: (d.name, d.address))
    return out
