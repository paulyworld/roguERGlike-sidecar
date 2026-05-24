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


async def test_inbound_annotate_is_published_as_rider_annotation_envelope() -> None:
    """An ``annotate`` command from a client is republished as a typed
    ``rider_annotation`` envelope. Verifies the schema bridge: client speaks
    "annotate", recorder + replay see "rider_annotation"."""
    import websockets

    from roguerglike_sidecar.events import Command, RiderAnnotationData
    from roguerglike_sidecar.ws_server import run_ws_server

    received: list[Command] = []

    async def handler(cmd: Command) -> None:
        received.append(cmd)

    bus = EventBus()
    async with (
        run_ws_server(bus, host="localhost", port=18423, on_command=handler),
        websockets.connect("ws://localhost:18423") as ws,
        bus.subscribe() as q,
    ):
        await ws.send(
            '{"type": "annotate", "tag": "ui-pause", '
            '"note": "between-round screen", "client_id": "concert-mvp"}'
        )
        env = await asyncio.wait_for(q.get(), timeout=1.0)

    assert env.type == "rider_annotation"
    assert env.device_kind == "client"
    assert isinstance(env.data, RiderAnnotationData)
    assert env.data.tag == "ui-pause"
    assert env.data.note == "between-round screen"
    assert env.data.client_id == "concert-mvp"
    # Annotations bypass on_command — handler must not receive them, otherwise
    # the CLI's trainer-control gate would accidentally see annotation traffic
    # and the contract would be confused.
    assert received == []


async def test_inbound_annotate_passes_client_time_and_context_through() -> None:
    """Hybrid-schema annotate command: client_time_s + context pass through
    unchanged into the rider_annotation envelope. Sidecar treats context as
    opaque — no validation inside the blob — so any client-supplied state
    lands in the recording verbatim."""
    import websockets

    from roguerglike_sidecar.events import RiderAnnotationData
    from roguerglike_sidecar.ws_server import run_ws_server

    bus = EventBus()
    async with (
        run_ws_server(bus, host="localhost", port=18425, on_command=None),
        websockets.connect("ws://localhost:18425") as ws,
        bus.subscribe() as q,
    ):
        await ws.send(
            '{"type": "annotate", "tag": "too-hard", '
            '"client_time_s": 2412.5, "client_id": "gizzERG", '
            '"context": {"mode": "terrain_erg", "grade": 5.5, '
            '"target_watts": 228, "section": "Motor Spirit"}}'
        )
        env = await asyncio.wait_for(q.get(), timeout=1.0)

    assert env.type == "rider_annotation"
    assert isinstance(env.data, RiderAnnotationData)
    assert env.data.tag == "too-hard"
    assert env.data.client_time_s == 2412.5
    assert env.data.context is not None
    assert env.data.context["mode"] == "terrain_erg"
    assert env.data.context["target_watts"] == 228


async def test_inbound_annotate_works_without_trainer_control_handler() -> None:
    """Annotations don't touch the trainer, so they must work even when the
    sidecar was launched without ``--allow-trainer-control`` (i.e.
    ``on_command is None``). This is the path concert-mvp uses for off-bike
    mock-mode dev."""
    import websockets

    from roguerglike_sidecar.events import RiderAnnotationData
    from roguerglike_sidecar.ws_server import run_ws_server

    bus = EventBus()
    async with (
        run_ws_server(bus, host="localhost", port=18424, on_command=None),
        websockets.connect("ws://localhost:18424") as ws,
        bus.subscribe() as q,
    ):
        await ws.send('{"type": "annotate", "tag": "marker"}')
        env = await asyncio.wait_for(q.get(), timeout=1.0)

    assert env.type == "rider_annotation"
    assert isinstance(env.data, RiderAnnotationData)
    assert env.data.tag == "marker"


async def test_hello_envelope_replays_to_late_subscribers() -> None:
    """``hello`` is the sidecar self-describe; clients connecting any time
    during a session must learn the protocol version + feature list. Test
    confirms it lands via the session-state replay path."""
    from roguerglike_sidecar.events import HelloData
    from roguerglike_sidecar.session import announce_hello

    bus = EventBus()
    await announce_hello(bus, mode="mock", features=["set_target_power", "annotations"])
    # Other session-state events fire after hello in normal startup; confirm
    # hello still replays in its own seq slot regardless.
    await bus.publish(
        type_="device_connected",
        data=DeviceConnectedData(kind="bike_trainer", name="KICKR"),
        device_kind="mock",
    )

    async with bus.subscribe() as q:
        first = await asyncio.wait_for(q.get(), timeout=1.0)
        second = await asyncio.wait_for(q.get(), timeout=1.0)

    # Replayed in seq order: hello first (seq=0), device_connected second (seq=1).
    assert first.type == "hello"
    assert isinstance(first.data, HelloData)
    assert first.data.mode == "mock"
    assert "annotations" in first.data.features
    assert first.seq == 0
    assert second.type == "device_connected"
    assert second.seq == 1


async def test_announce_hello_uses_sidecar_device_kind() -> None:
    """Hello isn't from a piece of hardware — it's the sidecar describing
    itself. ``device_kind`` should be ``sidecar``, distinct from the
    ``client`` kind used for rider-originated events."""
    from roguerglike_sidecar.session import announce_hello

    bus = EventBus()
    async with bus.subscribe() as q:
        await announce_hello(bus, mode="live", features=[])
        env = await asyncio.wait_for(q.get(), timeout=1.0)

    assert env.device_kind == "sidecar"


async def test_build_features_includes_baseline_capabilities() -> None:
    """build_features advertises set_target_power even without
    --allow-trainer-control: the command exists in the protocol; runtime
    gating surfaces via the typed target_power_set rejection, not via
    feature absence. Otherwise a client that connects before the operator
    flips the flag would gate UI off and never re-enable it."""
    from roguerglike_sidecar.session import build_features

    flagged = build_features(allow_trainer_control=True)
    unflagged = build_features(allow_trainer_control=False)
    for feature in ("set_target_power", "recording", "annotations"):
        assert feature in flagged
        assert feature in unflagged


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
