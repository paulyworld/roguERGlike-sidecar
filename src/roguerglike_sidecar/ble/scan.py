"""Scan for BLE devices advertising one or more profile service UUIDs.

Thin wrapper over ``BleakScanner.discover`` so the CLI / future pairing UI
can list candidate devices per profile without standing up a full source.

We deliberately scan **unfiltered** and post-filter in Python rather than
passing ``service_uuids=`` to Bleak. Bleak's adapter-level filter is
unreliable on Windows: devices that advertise their target UUID in the
*scan response* (rather than the initial advertisement packet) are silently
dropped. KICKRs (FTMS) do this and some HR sensors do too. Post-filtering
catches them — see memory ``bleak-windows-scan-filter-bug``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .profile import BleProfile

log = logging.getLogger(__name__)

DEFAULT_SCAN_TIMEOUT_S = 8.0


@dataclass(frozen=True)
class DiscoveredDevice:
    address: str
    name: str
    rssi: int | None


async def scan_for_profiles(
    profiles: list[BleProfile],
    *,
    timeout_s: float = DEFAULT_SCAN_TIMEOUT_S,
) -> dict[str, list[DiscoveredDevice]]:
    """One unfiltered BLE scan, results categorized by matching profile.

    Returns a dict keyed by ``profile.name`` (in the order of the ``profiles``
    argument), each value a name-sorted list of devices that advertise that
    profile's service. A device advertising multiple matching services (rare
    but valid — e.g. a power meter that exposes both FTMS and Cycling Power)
    shows up under each.
    """
    from bleak import BleakScanner  # local import keeps unit tests Bleak-free

    profile_by_uuid: dict[str, BleProfile] = {p.service_uuid.lower(): p for p in profiles}
    log.info(
        "scanning for %d profile(s) (%s) for %.1fs",
        len(profiles),
        ", ".join(p.name for p in profiles),
        timeout_s,
    )
    raw = await BleakScanner.discover(timeout=timeout_s, return_adv=True)

    out: dict[str, list[DiscoveredDevice]] = {p.name: [] for p in profiles}
    for address, (device, adv) in raw.items():
        advertised = {u.lower() for u in (adv.service_uuids or [])}
        for uuid in advertised:
            profile = profile_by_uuid.get(uuid)
            if profile is None:
                continue
            out[profile.name].append(
                DiscoveredDevice(
                    address=address,
                    name=device.name or adv.local_name or "(unnamed)",
                    rssi=adv.rssi,
                )
            )
    for devices in out.values():
        devices.sort(key=lambda d: (d.name, d.address))
    return out
