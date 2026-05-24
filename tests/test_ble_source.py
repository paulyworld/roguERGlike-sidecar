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


# --- distance delta tracking -----------------------------------------------


class _DistanceProfile:
    """Emits a distance event with caller-controlled meters_total."""

    name = "dist"
    service_uuid = "00000000-0000-0000-0000-000000000000"
    char_uuid = "00000001-0000-0000-0000-000000000000"
    device_kind: DeviceKind = "bike_trainer"

    def decode(self, payload: bytes) -> Iterable[tuple[EventType, EventData]]:
        from roguerglike_sidecar.events import DistanceData

        total = int.from_bytes(payload, "little")
        yield (
            "distance",
            DistanceData(meters_total=float(total), meters_delta=0.0, source="trainer"),
        )


async def test_first_distance_event_emits_zero_delta() -> None:
    """A trainer that's just connected has no prior reading; delta=0 prevents
    a spurious huge delta on the first event (the cumulative value could be
    in the kilometers from a prior un-cleared session)."""
    from roguerglike_sidecar.events import DistanceData

    bus = EventBus()
    source = BleSource(_DistanceProfile(), "addr", "DistTest", bus)
    async with bus.subscribe() as q:
        await source.on_packet((12500).to_bytes(4, "little"))
        env = await asyncio.wait_for(q.get(), timeout=1.0)
    assert env.type == "distance"
    assert isinstance(env.data, DistanceData)
    assert env.data.meters_total == 12500.0
    assert env.data.meters_delta == 0.0
    assert env.data.source == "trainer"


async def test_subsequent_distance_events_compute_delta_from_prior() -> None:
    """meters_delta = current_total - prior_total. Lets analyzers integrate
    distance per-tick rather than diffing the cumulative themselves."""
    from roguerglike_sidecar.events import DistanceData

    bus = EventBus()
    source = BleSource(_DistanceProfile(), "addr", "DeltaTest", bus)
    async with bus.subscribe() as q:
        await source.on_packet((12500).to_bytes(4, "little"))
        first = await asyncio.wait_for(q.get(), timeout=1.0)
        await source.on_packet((12515).to_bytes(4, "little"))
        second = await asyncio.wait_for(q.get(), timeout=1.0)
        await source.on_packet((12530).to_bytes(4, "little"))
        third = await asyncio.wait_for(q.get(), timeout=1.0)

    assert isinstance(first.data, DistanceData)
    assert isinstance(second.data, DistanceData)
    assert isinstance(third.data, DistanceData)
    assert first.data.meters_delta == 0.0
    assert second.data.meters_delta == 15.0
    assert third.data.meters_delta == 15.0


async def test_distance_counter_reset_emits_zero_delta_not_negative() -> None:
    """A trainer that resets its counter (rare but possible — e.g. mid-ride
    BLE re-pair) sends a smaller cumulative than last seen. Without
    protection, meters_delta would go negative and the schema would reject.
    Treat any backward step as a fresh series (delta=0)."""
    from roguerglike_sidecar.events import DistanceData

    bus = EventBus()
    source = BleSource(_DistanceProfile(), "addr", "ResetTest", bus)
    async with bus.subscribe() as q:
        await source.on_packet((12500).to_bytes(4, "little"))
        await asyncio.wait_for(q.get(), timeout=1.0)
        await source.on_packet((50).to_bytes(4, "little"))  # huge backward jump
        env = await asyncio.wait_for(q.get(), timeout=1.0)

    assert isinstance(env.data, DistanceData)
    assert env.data.meters_total == 50.0
    assert env.data.meters_delta == 0.0  # NOT -12450
