"""EventBus seq monotonicity + subscriber fan-out."""

from __future__ import annotations

import asyncio

import pytest
from roguerglike_sidecar.events import CadenceData, PowerData
from roguerglike_sidecar.ws_server import EventBus


async def test_seq_is_monotonic_and_starts_at_zero() -> None:
    bus = EventBus()
    envs = []
    for w in (100, 150, 200, 250):
        envs.append(await bus.publish(type_="power", data=PowerData(watts=w), device_kind="mock"))
    assert [e.seq for e in envs] == [0, 1, 2, 3]
    assert all(e.session_id == bus.session_id for e in envs)


async def test_subscribers_receive_events_in_order() -> None:
    bus = EventBus()
    async with bus.subscribe() as q:
        await bus.publish(type_="power", data=PowerData(watts=200), device_kind="mock")
        await bus.publish(type_="cadence", data=CadenceData(rpm=90), device_kind="mock")
        first = await asyncio.wait_for(q.get(), timeout=1.0)
        second = await asyncio.wait_for(q.get(), timeout=1.0)
        assert (first.type, second.type) == ("power", "cadence")
        assert first.seq == 0 and second.seq == 1


async def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    async with bus.subscribe() as q:
        await bus.publish(type_="power", data=PowerData(watts=50), device_kind="mock")
        await asyncio.wait_for(q.get(), timeout=1.0)
    # After context exit, the queue is no longer in the subscriber set.
    await bus.publish(type_="power", data=PowerData(watts=60), device_kind="mock")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q.get(), timeout=0.05)
