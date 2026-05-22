"""CadenceBailout state machine tests.

The bailout has three observable behaviours:

1. **Engage**: after sustained low cadence at a meaningful target, target drops
   to the configured floor and a ``cadence_bailout_engaged`` envelope fires.
2. **Resume ramp**: when cadence comes back, a sequence of ``set_target_power``
   writes ramps the trainer from the floor back to the pre-pause target, then
   a ``cadence_bailout_disengaged`` envelope fires.
3. **Pause-time queueing**: a ``set_target_power`` command issued during the
   paused window doesn't write to the trainer; it updates the pre-pause
   target so the resume restores to the new value, and the engine sees a
   ``target_power_set accepted=false reason='bailout-pending'`` ack.

Plus the curve helpers (intensity → bailout / ramp duration) for confidence
that the maths line up with the project-memory anchors.
"""

from __future__ import annotations

import asyncio
import struct
import time
from typing import Any

import pytest

from roguerglike_sidecar.ble.cadence_bailout import (
    ACTIVE_CADENCE_RPM,
    BAILOUT_S_AT_HIGH_PCT,
    BAILOUT_S_AT_LOW_PCT,
    RAMP_S_AT_HIGH_PCT,
    RAMP_S_AT_LOW_PCT,
    CadenceBailout,
    _interp_for_pct_ftp,
)
from roguerglike_sidecar.ble.ftms_control import (
    CONTROL_POINT_CHAR_UUID,
    FtmsControl,
)
from roguerglike_sidecar.events import (
    CadenceBailoutDisengagedData,
    CadenceBailoutEngagedData,
    Envelope,
    TargetPowerSetData,
)
from roguerglike_sidecar.ws_server import EventBus

# --- curve helpers ----------------------------------------------------------


def test_intensity_curve_anchors() -> None:
    """At LOW pct, curve returns low_value; at HIGH pct, high_value;
    clamped outside [LOW, HIGH]."""
    assert _interp_for_pct_ftp(90, 15, 0.5) == 90  # exact LOW
    assert _interp_for_pct_ftp(90, 15, 1.5) == 15  # exact HIGH
    assert _interp_for_pct_ftp(90, 15, 0.0) == 90  # below LOW clamps
    assert _interp_for_pct_ftp(90, 15, 2.0) == 15  # above HIGH clamps


def test_intensity_curve_midpoint() -> None:
    """At pct_ftp = 1.0 (midway between LOW=0.5 and HIGH=1.5), the curve sits
    at the arithmetic mean of the endpoints."""
    bailout_at_threshold = _interp_for_pct_ftp(BAILOUT_S_AT_LOW_PCT, BAILOUT_S_AT_HIGH_PCT, 1.0)
    ramp_at_threshold = _interp_for_pct_ftp(RAMP_S_AT_LOW_PCT, RAMP_S_AT_HIGH_PCT, 1.0)
    assert bailout_at_threshold == (BAILOUT_S_AT_LOW_PCT + BAILOUT_S_AT_HIGH_PCT) / 2
    assert ramp_at_threshold == (RAMP_S_AT_LOW_PCT + RAMP_S_AT_HIGH_PCT) / 2


# --- mock client + helpers -------------------------------------------------


class _MockClient:
    """Mirrors the one in test_ftms_control.py: records writes, schedules
    deterministic SUCCESS indications per write."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self._handler: Any = None

    async def start_notify(self, _uuid: str, handler: Any) -> None:
        self._handler = handler

    async def write_gatt_char(
        self,
        uuid: str,
        data: bytes,
        *,
        response: bool = True,  # noqa: ARG002
    ) -> None:
        assert uuid == CONTROL_POINT_CHAR_UUID
        self.writes.append(bytes(data))
        opcode = data[0]

        async def _fire() -> None:
            await asyncio.sleep(0)
            if self._handler is not None:
                self._handler(None, bytearray([0x80, opcode, 0x01]))  # SUCCESS

        asyncio.create_task(_fire())


async def _make_control(
    *, min_w: int = 0, max_w: int = 800
) -> tuple[FtmsControl, _MockClient, EventBus]:
    bus = EventBus()
    client = _MockClient()
    ctrl = FtmsControl(
        client,  # type: ignore[arg-type]
        bus,
        device_kind="bike_trainer",
        device_name="MockKICKR",
        max_watts=max_w,
        min_watts=min_w,
    )
    await ctrl.attach()
    await ctrl.request_control_and_start()
    return ctrl, client, bus


async def _drain_until(
    q: asyncio.Queue[Envelope], type_: str, *, timeout_s: float = 1.0
) -> Envelope:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"no {type_} event in {timeout_s}s")
        env = await asyncio.wait_for(q.get(), timeout=remaining)
        if env.type == type_:
            return env


def _set_target_writes(client: _MockClient) -> list[int]:
    """Return the int16 watts values of every Set Target Power write so far."""
    out: list[int] = []
    for w in client.writes:
        if len(w) >= 3 and w[0] == 0x05:  # OP_SET_TARGET_POWER
            out.append(struct.unpack_from("<h", w, 1)[0])
    return out


# --- engage path -----------------------------------------------------------


async def test_engage_publishes_envelope_and_drops_target_to_min() -> None:
    """When _engage_pause() is called, the trainer write goes to min_watts
    and a cadence_bailout_engaged envelope fires."""
    ctrl, client, bus = await _make_control(min_w=20)
    await ctrl.set_target_power(250)

    bailout = CadenceBailout(
        ctrl,
        bus,
        device_kind="bike_trainer",
        device_name="MockKICKR",
        static_bailout_s=60.0,
        static_ramp_s=3.0,
    )

    async with bus.subscribe() as q:
        await bailout._engage_pause()  # noqa: SLF001 — direct exercise of the path
        env = await _drain_until(q, "cadence_bailout_engaged")

    assert bailout.is_paused
    assert bailout.pre_pause_target_watts == 250
    assert isinstance(env.data, CadenceBailoutEngagedData)
    assert env.data.pre_pause_target_watts == 250
    # Last write should be Set Target Power(min_watts=20).
    assert _set_target_writes(client)[-1] == 20


async def test_does_not_engage_when_target_is_already_at_floor() -> None:
    """The watcher loop should skip pause if last_target == min_watts (nothing
    to bail out from). Exercised indirectly via a very short bailout window."""
    ctrl, client, bus = await _make_control(min_w=100)
    await ctrl.set_target_power(100)  # already at floor

    bailout = CadenceBailout(
        ctrl,
        bus,
        device_kind="bike_trainer",
        device_name="MockKICKR",
        static_bailout_s=0.05,  # extremely tight
        static_ramp_s=0.0,
    )
    # Run only the watcher loop briefly; no cadence events, so idle_s grows.
    task = asyncio.create_task(bailout._watcher_loop())  # noqa: SLF001
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not bailout.is_paused


# --- pause-window queueing -------------------------------------------------


async def test_set_target_power_during_pause_queues_and_acks_pending() -> None:
    """A command issued during pause must NOT write to the trainer; it must
    update pre_pause_target so the resume restores correctly; and it must
    fire a target_power_set ack with accepted=false reason='bailout-pending'
    so the engine can distinguish queued from applied."""
    ctrl, client, bus = await _make_control(min_w=20)
    await ctrl.set_target_power(250)

    bailout = CadenceBailout(
        ctrl,
        bus,
        device_kind="bike_trainer",
        device_name="MockKICKR",
    )
    await bailout._engage_pause()  # noqa: SLF001
    writes_before = len(_set_target_writes(client))

    async with bus.subscribe() as q:
        await bailout.handle_set_target_power(310)
        env = await _drain_until(q, "target_power_set")

    # pre_pause_target updated to the new value
    assert bailout.pre_pause_target_watts == 310
    # No additional write reached the trainer (still paused, hasn't resumed)
    assert len(_set_target_writes(client)) == writes_before
    # Engine sees an explicit pending ack
    assert isinstance(env.data, TargetPowerSetData)
    assert env.data.accepted is False
    assert "bailout-pending" in env.data.reason
    assert env.data.watts == 310


async def test_set_target_power_when_not_paused_passes_through() -> None:
    """In the normal path, handle_set_target_power should call straight
    through to FtmsControl.set_target_power and the engine should see an
    accepted=true ack."""
    ctrl, client, bus = await _make_control()
    await ctrl.set_target_power(150)
    writes_before = len(_set_target_writes(client))

    bailout = CadenceBailout(
        ctrl,
        bus,
        device_kind="bike_trainer",
        device_name="MockKICKR",
    )

    async with bus.subscribe() as q:
        await bailout.handle_set_target_power(220)
        env = await _drain_until(q, "target_power_set")

    assert env.data.accepted is True  # type: ignore[union-attr]
    assert env.data.watts == 220  # type: ignore[union-attr]
    assert _set_target_writes(client)[-1] == 220
    assert len(_set_target_writes(client)) == writes_before + 1


# --- resume ramp -----------------------------------------------------------


async def test_resume_on_active_cadence_ramps_back_to_pre_pause_target() -> None:
    """After engage, an on_cadence call with rpm >= ACTIVE_CADENCE_RPM kicks
    off the resume ramp. The ramp eventually writes pre_pause_target and
    fires a cadence_bailout_disengaged envelope."""
    ctrl, client, bus = await _make_control(min_w=20)
    await ctrl.set_target_power(200)

    bailout = CadenceBailout(
        ctrl,
        bus,
        device_kind="bike_trainer",
        device_name="MockKICKR",
        static_bailout_s=60.0,
        static_ramp_s=0.05,  # snap through the ramp fast in tests
    )
    await bailout._engage_pause()  # noqa: SLF001

    async with bus.subscribe() as q:
        await bailout._on_cadence(ACTIVE_CADENCE_RPM)  # noqa: SLF001 — direct trigger
        # Wait for the ramp task to publish disengaged.
        env = await _drain_until(q, "cadence_bailout_disengaged", timeout_s=2.0)
        # Let any in-flight ramp tasks settle.
        if bailout._ramp_task:  # noqa: SLF001
            async with asyncio.timeout(1.0):
                await bailout._ramp_task  # noqa: SLF001

    assert not bailout.is_paused
    assert isinstance(env.data, CadenceBailoutDisengagedData)
    assert env.data.restored_to_watts == 200
    # Final write should be Set Target Power(200).
    assert _set_target_writes(client)[-1] == 200


async def test_resume_ramp_progresses_through_intermediate_values() -> None:
    """With ramp_s > one tick, the ramp emits intermediate Set Target Power
    writes between the floor and pre_pause_target. Quick proof: pre-pause
    target far above floor + non-zero ramp_s yields > 1 set_target write
    after resume."""
    ctrl, client, bus = await _make_control(min_w=20)
    await ctrl.set_target_power(320)

    bailout = CadenceBailout(
        ctrl,
        bus,
        device_kind="bike_trainer",
        device_name="MockKICKR",
        static_bailout_s=60.0,
        static_ramp_s=1.0,  # ~2 ticks at RAMP_TICK_S=0.5
    )
    await bailout._engage_pause()  # noqa: SLF001 — pre-pause target = 320; write to floor
    writes_after_engage = len(_set_target_writes(client))

    await bailout._on_cadence(ACTIVE_CADENCE_RPM)  # noqa: SLF001
    if bailout._ramp_task:  # noqa: SLF001
        async with asyncio.timeout(2.0):
            await bailout._ramp_task  # noqa: SLF001

    ramp_writes = _set_target_writes(client)[writes_after_engage:]
    # Should be at least 2 writes (intermediate + final), strictly increasing
    # from floor toward pre_pause_target.
    assert len(ramp_writes) >= 2
    assert ramp_writes[-1] == 320


async def test_late_engine_command_during_pause_changes_restore_target() -> None:
    """Engine asks for 400W mid-pause; resume should ramp to 400W, not back
    to the original 250W."""
    ctrl, client, bus = await _make_control(min_w=20)
    await ctrl.set_target_power(250)

    bailout = CadenceBailout(
        ctrl,
        bus,
        device_kind="bike_trainer",
        device_name="MockKICKR",
        static_bailout_s=60.0,
        static_ramp_s=0.05,
    )
    await bailout._engage_pause()  # noqa: SLF001 — pre_pause_target = 250
    await bailout.handle_set_target_power(400)  # engine updates target during pause

    assert bailout.pre_pause_target_watts == 400

    await bailout._on_cadence(ACTIVE_CADENCE_RPM)  # noqa: SLF001
    if bailout._ramp_task:  # noqa: SLF001
        async with asyncio.timeout(2.0):
            await bailout._ramp_task  # noqa: SLF001

    # Final write should restore to the engine's NEW target, not the original.
    assert _set_target_writes(client)[-1] == 400


# --- end-to-end via the watcher loop ---------------------------------------


async def test_watcher_engages_after_static_bailout_window() -> None:
    """The full state machine end-to-end: feed ONE cadence=0 event then let
    the watcher loop's idle timer fire. With static_bailout_s=0.2 the pause
    should engage within ~0.3s."""
    ctrl, client, bus = await _make_control(min_w=20)
    await ctrl.set_target_power(200)

    bailout = CadenceBailout(
        ctrl,
        bus,
        device_kind="bike_trainer",
        device_name="MockKICKR",
        static_bailout_s=0.2,
        static_ramp_s=0.05,
    )
    # Start the bailout's full machinery (subscriber + watcher).
    run_task = asyncio.create_task(bailout.run())

    try:
        async with bus.subscribe() as q:
            # Publish a cadence=0 — confirms the cadence stream is being consumed
            # AND keeps _last_active_ts stale.
            await bus.publish(
                type_="cadence",
                data={"rpm": 0},  # type: ignore[arg-type]
                device_kind="bike_trainer",
            )
            env = await _drain_until(q, "cadence_bailout_engaged", timeout_s=2.0)
    finally:
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task

    assert isinstance(env.data, CadenceBailoutEngagedData)
    assert env.data.pre_pause_target_watts == 200


# --- bug A: idle timer resets on first meaningful target ----------------


async def test_handle_set_target_power_resets_idle_timer_for_non_floor() -> None:
    """The original bailout bug: _last_active_ts initialized at __init__
    measured "time since sidecar startup" rather than "time since rider
    stopped at a meaningful target". Operator delay between sidecar launch
    and pressing Start Workout → bailout fires on the first warmup target.

    Fix: handle_set_target_power refreshes _last_active_ts when a non-floor
    target is being written. The idle window then correctly measures "time
    since last cadence event OR last meaningful target" — the bailout's
    intended semantic."""
    ctrl, _client, bus = await _make_control(min_w=20)
    bailout = CadenceBailout(
        ctrl,
        bus,
        device_kind="bike_trainer",
        device_name="MockKICKR",
    )
    # Simulate "operator opened sidecar long ago"
    stale_ts = time.monotonic() - 1000.0
    bailout._last_active_ts = stale_ts  # noqa: SLF001

    # Routing a non-floor target through must refresh the timer.
    await bailout.handle_set_target_power(150)
    assert bailout._last_active_ts > stale_ts + 999  # noqa: SLF001 — was just reset


async def test_handle_set_target_power_does_not_reset_for_floor_target() -> None:
    """A target at or below the configured floor is "release" / "free spin"
    semantics, not "rider is at a meaningful target". Don't reset the idle
    timer for these — they shouldn't keep the bailout disarmed."""
    ctrl, _client, bus = await _make_control(min_w=20)
    bailout = CadenceBailout(
        ctrl,
        bus,
        device_kind="bike_trainer",
        device_name="MockKICKR",
    )
    stale_ts = time.monotonic() - 1000.0
    bailout._last_active_ts = stale_ts  # noqa: SLF001

    await bailout.handle_set_target_power(20)  # equals min_w
    assert bailout._last_active_ts == stale_ts  # noqa: SLF001 — unchanged


async def test_first_meaningful_target_after_long_idle_doesnt_immediately_engage() -> None:
    """End-to-end check that the bug-A regression is gone: stale _last_active_ts
    + first non-floor target via handle_set_target_power → watcher's next
    tick must NOT engage the bailout."""
    ctrl, _client, bus = await _make_control(min_w=20)
    bailout = CadenceBailout(
        ctrl,
        bus,
        device_kind="bike_trainer",
        device_name="MockKICKR",
        static_bailout_s=0.1,  # very tight window — would trip without the fix
    )
    bailout._last_active_ts = time.monotonic() - 1000.0  # noqa: SLF001

    await bailout.handle_set_target_power(200)
    # Watcher tick after the timer reset shouldn't engage.
    task = asyncio.create_task(bailout._watcher_loop())  # noqa: SLF001
    await asyncio.sleep(0.05)  # let one watcher iteration pass
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not bailout.is_paused


# --- silent-rejection fix (ws_server) -------------------------------------


async def test_no_handler_set_target_power_publishes_rejection_envelope() -> None:
    """Sidecar without --allow-trainer-control: WS server gets a
    set_target_power command but has no handler. Engine must see a
    target_power_set accepted=false rejection rather than nothing."""
    import websockets

    from roguerglike_sidecar.ws_server import run_ws_server

    bus = EventBus()
    async with (
        run_ws_server(bus, host="localhost", port=18431, on_command=None),
        websockets.connect("ws://localhost:18431") as ws,
    ):
        await ws.send('{"type": "set_target_power", "watts": 200}')
        # Read events until we see the rejection. There may be no prior
        # session-state events on this bus, so the first one out should be
        # ours.
        for _ in range(20):
            import json

            msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
            env = json.loads(msg)
            if env["type"] == "target_power_set":
                assert env["data"]["accepted"] is False
                assert "trainer-control disabled" in env["data"]["reason"]
                assert env["data"]["watts"] == 200
                return
        raise AssertionError("never saw target_power_set rejection")
