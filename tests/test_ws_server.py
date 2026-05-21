"""EventBus seq monotonicity + subscriber fan-out."""

from __future__ import annotations

import asyncio

import pytest
from roguerglike_sidecar.events import (
    CadenceData,
    DeviceConnectedData,
    PowerData,
    SessionStartData,
)
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


async def test_late_subscriber_receives_session_state_replay() -> None:
    """Engine-style client that connects after the producer has announced the
    session and device must still learn the session id and device name."""
    bus = EventBus()
    await bus.publish(
        type_="session_start",
        data=SessionStartData(session_id=bus.session_id),
        device_kind="mock",
    )
    await bus.publish(
        type_="device_connected",
        data=DeviceConnectedData(kind="bike_trainer", name="KICKR"),
        device_kind="mock",
    )
    await bus.publish(type_="power", data=PowerData(watts=200), device_kind="mock")

    async with bus.subscribe() as q:
        # Live event published after subscribe must come after the replayed pair.
        await bus.publish(type_="cadence", data=CadenceData(rpm=90), device_kind="mock")

        replay_a = await asyncio.wait_for(q.get(), timeout=1.0)
        replay_b = await asyncio.wait_for(q.get(), timeout=1.0)
        live = await asyncio.wait_for(q.get(), timeout=1.0)

    assert replay_a.type == "session_start"
    assert replay_b.type == "device_connected"
    assert (replay_a.seq, replay_b.seq) == (0, 1)  # original seqs preserved
    assert live.type == "cadence" and live.seq == 3
    # `power` (a non-session-state event) is not replayed.


async def test_early_subscriber_does_not_get_replay_duplicate() -> None:
    """A subscriber present before session_start must receive each event exactly
    once — no duplicate from the replay path."""
    bus = EventBus()
    async with bus.subscribe() as q:
        await bus.publish(
            type_="session_start",
            data=SessionStartData(session_id=bus.session_id),
            device_kind="mock",
        )
        first = await asyncio.wait_for(q.get(), timeout=1.0)
        assert first.type == "session_start"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.get(), timeout=0.05)


async def test_latest_session_state_wins_on_replay() -> None:
    """If device_connected is emitted twice (e.g. reconnect), the late
    subscriber sees only the most recent."""
    bus = EventBus()
    await bus.publish(
        type_="device_connected",
        data=DeviceConnectedData(kind="bike_trainer", name="OLD"),
        device_kind="mock",
    )
    await bus.publish(
        type_="device_connected",
        data=DeviceConnectedData(kind="bike_trainer", name="NEW"),
        device_kind="mock",
    )
    async with bus.subscribe() as q:
        env = await asyncio.wait_for(q.get(), timeout=1.0)
        assert isinstance(env.data, DeviceConnectedData)
        assert env.data.name == "NEW"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.get(), timeout=0.05)
