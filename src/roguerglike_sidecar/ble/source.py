"""BleSource — one BLE connection driving the shared EventBus.

Owns the connect / notify / disconnect lifecycle for a single device. The
notification handler is async; each payload is run through the profile's
decoder and every decoded event is published to the bus. Multiple sources can
run concurrently (bike + chest strap + ...), each independent.

Reconnect is best-effort: on any error or disconnect, the source publishes
``device_disconnected`` and waits a short backoff before attempting again.
``run`` only exits on cancellation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from ..events import (
    DeviceCapabilitiesData,
    DeviceConnectedData,
    DeviceDisconnectedData,
    EventType,
)
from ..ws_server import EventBus
from .profile import BleProfile

if TYPE_CHECKING:
    from bleak import BleakClient

log = logging.getLogger(__name__)

RECONNECT_BACKOFF_S = 5.0


class BleSource:
    """Bind one ``BleProfile`` to one device address and feed an ``EventBus``."""

    def __init__(
        self,
        profile: BleProfile,
        address: str,
        name: str,
        bus: EventBus,
        *,
        drop_event_types: frozenset[EventType] = frozenset(),
    ) -> None:
        """``drop_event_types`` suppresses publishing of specific event types
        decoded from this source's packets. Used for HR de-duplication when a
        chest strap and a bike that embeds HR are both paired: the bike's
        source is configured with ``drop_event_types={"heart_rate"}`` so the
        strap is the single source of truth on the wire."""
        self._profile = profile
        self._address = address
        self._name = name
        self._bus = bus
        self._drop_event_types = drop_event_types

    async def on_packet(self, payload: bytes) -> None:
        """Decode one notification payload and publish each resulting event.

        Public so tests can drive it without standing up a real BLE client.
        """
        for type_, data in self._profile.decode(payload):
            if type_ in self._drop_event_types:
                continue
            await self._bus.publish(
                type_=type_,
                data=data,
                device_kind=self._profile.device_kind,
            )

    async def _publish_connected(self) -> None:
        await self._bus.publish(
            type_="device_connected",
            data=DeviceConnectedData(kind=self._profile.device_kind, name=self._name),
            device_kind=self._profile.device_kind,
        )

    async def _publish_disconnected(self) -> None:
        await self._bus.publish(
            type_="device_disconnected",
            data=DeviceDisconnectedData(kind=self._profile.device_kind, name=self._name),
            device_kind=self._profile.device_kind,
        )

    async def _read_and_publish_capabilities(self, client: BleakClient) -> None:
        """If the profile exposes a feature characteristic, read it once and
        emit a ``device_capabilities`` event. Soft failure: log + continue, so
        a device that advertises FTMS but rejects the feature read doesn't
        kill the source. Most FTMS bikes return the feature char fine; some
        cheaper trainers omit it."""
        feature_uuid = getattr(self._profile, "feature_char_uuid", None)
        if feature_uuid is None:
            return
        parse = getattr(self._profile, "parse_features", None)
        if parse is None:
            return
        try:
            payload = bytes(await client.read_gatt_char(feature_uuid))
        except Exception:  # noqa: BLE001 — capabilities are best-effort
            log.exception(
                "BLE source %s: failed to read feature characteristic; "
                "treating as no advertised capabilities",
                self._profile.name,
            )
            return
        caps = parse(payload)
        await self._bus.publish(
            type_="device_capabilities",
            data=DeviceCapabilitiesData(
                kind=self._profile.device_kind,
                name=self._name,
                **caps,
            ),
            device_kind=self._profile.device_kind,
        )

    async def run(self) -> None:
        """Connect, subscribe, stay subscribed until cancelled or disconnected;
        reconnect with backoff on failure."""
        from bleak import BleakClient  # local import — keeps test paths Bleak-free

        async def handler(_sender: Any, data: bytearray) -> None:
            await self.on_packet(bytes(data))

        while True:
            client: BleakClient | None = None
            connected = False
            try:
                client = BleakClient(self._address)
                await client.connect()
                connected = True
                log.info("BLE source %s connected to %s", self._profile.name, self._name)
                await self._publish_connected()
                await self._read_and_publish_capabilities(client)
                await client.start_notify(self._profile.char_uuid, handler)
                while client.is_connected:
                    await asyncio.sleep(1.0)
                log.info("BLE source %s lost connection", self._profile.name)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — log + back off, don't crash sidecar
                log.exception("BLE source %s error", self._profile.name)
            finally:
                if client is not None:
                    # Disconnect on an already-dead client can raise; we don't care.
                    with contextlib.suppress(Exception):
                        await client.disconnect()
                if connected:
                    await self._publish_disconnected()
            await asyncio.sleep(RECONNECT_BACKOFF_S)
