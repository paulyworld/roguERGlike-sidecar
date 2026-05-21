"""BleSource.on_packet wiring: decode + publish to the shared EventBus.

The real ``run`` method requires a Bleak client and a paired device; not
exercised in unit tests. ``on_packet`` is the seam where decoded events meet
the bus, and it's pure-async — testable without Bluetooth.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

import pytest

from roguerglike_sidecar.ble.source import BleSource
from roguerglike_sidecar.events import (
    CadenceData,
    DeviceKind,
    EventData,
    EventType,
    PowerData,
)
from roguerglike_sidecar.ws_server import EventBus


class _DummyProfile:
    """Trivial profile that emits one event per call, varying by payload."""

    name = "dummy"
    service_uuid = "00000000-0000-0000-0000-000000000000"
    char_uuid = "00000001-0000-0000-0000-000000000000"
    device_kind: DeviceKind = "bike_trainer"

    def decode(self, payload: bytes) -> Iterable[tuple[EventType, EventData]]:
        # one power per packet, watts = len(payload), so test asserts are tight
        yield ("power", PowerData(watts=len(payload)))


class _MultiEventProfile:
    """Emits two events per call, to confirm on_packet publishes each."""

    name = "multi"
    service_uuid = "00000000-0000-0000-0000-000000000000"
    char_uuid = "00000002-0000-0000-0000-000000000000"
    device_kind: DeviceKind = "bike_trainer"

    def decode(self, payload: bytes) -> Iterable[tuple[EventType, EventData]]:
        yield ("power", PowerData(watts=100))
        yield ("cadence", CadenceData(rpm=80))


async def test_on_packet_publishes_one_event_per_decoded_tuple() -> None:
    bus = EventBus()
    source = BleSource(_DummyProfile(), "addr", "Test Device", bus)
    async with bus.subscribe() as q:
        await source.on_packet(b"\x00\x00\x00")  # len=3 -> watts=3
        env = await asyncio.wait_for(q.get(), timeout=1.0)
    assert env.type == "power"
    assert isinstance(env.data, PowerData)
    assert env.data.watts == 3
    assert env.device_kind == "bike_trainer"


async def test_on_packet_publishes_every_decoded_event_in_order() -> None:
    bus = EventBus()
    source = BleSource(_MultiEventProfile(), "addr", "Test Device", bus)
    async with bus.subscribe() as q:
        await source.on_packet(b"\x00")
        first = await asyncio.wait_for(q.get(), timeout=1.0)
        second = await asyncio.wait_for(q.get(), timeout=1.0)
    assert (first.type, second.type) == ("power", "cadence")
    assert first.seq == 0 and second.seq == 1


async def test_publish_connected_uses_profile_device_kind_and_given_name() -> None:
    bus = EventBus()
    source = BleSource(_DummyProfile(), "AA:BB:CC", "KICKR CORE 8B2A", bus)
    async with bus.subscribe() as q:
        await source._publish_connected()  # noqa: SLF001 — exercising the helper
        env = await asyncio.wait_for(q.get(), timeout=1.0)
    assert env.type == "device_connected"
    assert env.device_kind == "bike_trainer"
    assert env.data.kind == "bike_trainer"  # type: ignore[union-attr]
    assert env.data.name == "KICKR CORE 8B2A"  # type: ignore[union-attr]


async def test_drop_event_types_suppresses_matching_events() -> None:
    """When a bike that embeds HR is paired alongside a standalone HR sensor,
    the bike source is configured with ``drop_event_types={"heart_rate"}`` so
    the strap is the sole HR source on the wire. on_packet must skip the
    drop-typed events and publish everything else."""
    bus = EventBus()
    source = BleSource(
        _MultiEventProfile(),
        "AA:BB:CC",
        "DropTest",
        bus,
        drop_event_types=frozenset({"power"}),
    )
    async with bus.subscribe() as q:
        await source.on_packet(b"\x00")
        first = await asyncio.wait_for(q.get(), timeout=1.0)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.get(), timeout=0.05)
    # _MultiEventProfile yields ('power', ...) then ('cadence', ...); dropping
    # 'power' leaves only the cadence event reaching the bus.
    assert first.type == "cadence"


async def test_drop_event_types_default_is_empty() -> None:
    """No suppression unless explicitly configured."""
    bus = EventBus()
    source = BleSource(_MultiEventProfile(), "addr", "Default", bus)
    async with bus.subscribe() as q:
        await source.on_packet(b"\x00")
        first = await asyncio.wait_for(q.get(), timeout=1.0)
        second = await asyncio.wait_for(q.get(), timeout=1.0)
    assert (first.type, second.type) == ("power", "cadence")
