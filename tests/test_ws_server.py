"""EventBus seq monotonicity + subscriber fan-out."""

from __future__ import annotations

import asyncio

import pytest

from roguerglike_sidecar.events import (
    CadenceData,
    ControlAcquiredData,
    ControlReleasedData,
    DeviceCapabilitiesData,
    DeviceConnectedData,
    PowerData,
    SessionStartData,
    TargetPowerSetData,
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


async def test_late_subscriber_receives_device_capabilities_replay() -> None:
    """An engine that attaches after the sidecar has already read the FTMS
    Feature characteristic must still learn what the device can be asked to
    do — capability gating in the engine UI depends on it."""
    bus = EventBus()
    await bus.publish(
        type_="device_capabilities",
        data=DeviceCapabilitiesData(
            kind="bike_trainer",
            name="KICKR",
            target_power=True,
            indoor_bike_simulation=True,
        ),
        device_kind="bike_trainer",
    )
    async with bus.subscribe() as q:
        env = await asyncio.wait_for(q.get(), timeout=1.0)

    assert env.type == "device_capabilities"
    assert isinstance(env.data, DeviceCapabilitiesData)
    assert env.data.target_power is True


async def test_late_subscriber_receives_control_acquired_replay() -> None:
    """An engine that reconnects mid-session needs to know whether the sidecar
    is currently holding the trainer (so its ERG UI reflects reality)."""
    bus = EventBus()
    await bus.publish(
        type_="control_acquired",
        data=ControlAcquiredData(kind="bike_trainer", name="KICKR"),
        device_kind="bike_trainer",
    )
    async with bus.subscribe() as q:
        env = await asyncio.wait_for(q.get(), timeout=1.0)

    assert env.type == "control_acquired"


async def test_late_subscriber_sees_acquired_then_released_in_order() -> None:
    """If control was claimed and later released, both events replay in seq
    order. Net effect for the engine: "control was acquired then released —
    currently released", which matches reality."""
    bus = EventBus()
    await bus.publish(
        type_="control_acquired",
        data=ControlAcquiredData(kind="bike_trainer", name="KICKR"),
        device_kind="bike_trainer",
    )
    await bus.publish(
        type_="control_released",
        data=ControlReleasedData(kind="bike_trainer", name="KICKR", reason="stop"),
        device_kind="bike_trainer",
    )

    async with bus.subscribe() as q:
        first = await asyncio.wait_for(q.get(), timeout=1.0)
        second = await asyncio.wait_for(q.get(), timeout=1.0)

    assert first.type == "control_acquired"
    assert second.type == "control_released"
    assert first.seq < second.seq


async def test_target_power_set_is_not_replayed() -> None:
    """``target_power_set`` is an ack of a specific command, not ambient
    state. A late subscriber should not see a stale ack from a previous
    engine instance's command. The engine can re-issue set_target_power if
    it wants to assert the current target."""
    bus = EventBus()
    await bus.publish(
        type_="target_power_set",
        data=TargetPowerSetData(watts=235, accepted=True),
        device_kind="bike_trainer",
    )
    async with bus.subscribe() as q:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.get(), timeout=0.05)


async def test_subscriber_count_tracks_subscribe_lifecycle() -> None:
    """The live runner's disconnect-bailout watcher polls subscriber_count to
    know when to arm/disarm. Confirm the count tracks subscribe-as-context."""
    bus = EventBus()
    assert bus.subscriber_count == 0
    async with bus.subscribe():
        assert bus.subscriber_count == 1
        async with bus.subscribe():
            assert bus.subscriber_count == 2
        assert bus.subscriber_count == 1
    assert bus.subscriber_count == 0


# --- bidirectional WS: inbound commands ---------------------------------


async def test_inbound_set_target_power_invokes_on_command_handler() -> None:
    """End-to-end through ``run_ws_server``: a websockets client sends a
    valid SetTargetPower command; the configured on_command handler receives
    it. Exercises the recv loop, the parse layer, and the dispatch."""
    import websockets

    from roguerglike_sidecar.events import Command, SetTargetPowerCommand
    from roguerglike_sidecar.ws_server import run_ws_server

    received: list[Command] = []

    async def handler(cmd: Command) -> None:
        received.append(cmd)

    bus = EventBus()
    async with (
        run_ws_server(bus, host="localhost", port=18421, on_command=handler),
        websockets.connect("ws://localhost:18421") as ws,
    ):
        await ws.send('{"type": "set_target_power", "watts": 235}')
        # Give the recv loop a moment to dispatch
        for _ in range(20):
            if received:
                break
            await asyncio.sleep(0.05)

    assert len(received) == 1
    assert isinstance(received[0], SetTargetPowerCommand)
    assert received[0].watts == 235


async def test_inbound_malformed_command_is_dropped_without_killing_ws() -> None:
    """Bad commands log + drop. The WS connection survives so subsequent
    valid commands still dispatch."""
    import websockets

    from roguerglike_sidecar.events import Command, StopCommand
    from roguerglike_sidecar.ws_server import run_ws_server

    received: list[Command] = []

    async def handler(cmd: Command) -> None:
        received.append(cmd)

    bus = EventBus()
    async with (
        run_ws_server(bus, host="localhost", port=18422, on_command=handler),
        websockets.connect("ws://localhost:18422") as ws,
    ):
        # First message: garbage. Second: valid stop.
        await ws.send("not-json-at-all")
        await ws.send('{"type": "stop"}')
        for _ in range(20):
            if received:
                break
            await asyncio.sleep(0.05)

    assert len(received) == 1
    assert isinstance(received[0], StopCommand)


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
