"""FtmsControl unit tests against a mocked Bleak client.

The control client's contract: claim control (Request Control + Start), then
on demand send Set Target Power with the int16 clamped to configured bounds.
Each opcode is awaited against a matching response indication (opcode 0x80 +
request opcode + result code) on the same Control Point characteristic.

We mock the BleakClient with a tiny stand-in that records writes and schedules
a deterministic indication response per write. This exercises the protocol
shape without a real BLE adapter.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Any

import pytest

from roguerglike_sidecar.ble.ftms_control import (
    CONTROL_POINT_CHAR_UUID,
    OP_REQUEST_CONTROL,
    OP_SET_TARGET_POWER,
    OP_START,
    OP_STOP,
    FtmsControl,
)
from roguerglike_sidecar.events import TargetPowerSetData
from roguerglike_sidecar.ws_server import EventBus


class _MockClient:
    """Stand-in for ``bleak.BleakClient``. Records writes; schedules deterministic
    indication responses per write."""

    def __init__(
        self,
        *,
        default_result: int = 0x01,
        result_overrides: dict[int, int] | None = None,
        silent_opcodes: set[int] | None = None,
    ) -> None:
        self.default_result = default_result
        self.result_overrides = result_overrides or {}
        self.silent_opcodes = silent_opcodes or set()
        self.writes: list[bytes] = []
        self._handler: Any = None

    async def start_notify(self, _uuid: str, handler: Any) -> None:
        self._handler = handler

    async def write_gatt_char(
        self,
        uuid: str,
        data: bytes,
        *,
        response: bool = True,  # noqa: ARG002 — accept like real Bleak
    ) -> None:
        assert uuid == CONTROL_POINT_CHAR_UUID
        self.writes.append(bytes(data))
        opcode = data[0]
        if opcode in self.silent_opcodes:
            return  # caller will see TimeoutError on the awaiting future
        result = self.result_overrides.get(opcode, self.default_result)

        async def _fire() -> None:
            # Defer one tick so the future is registered before we resolve it.
            await asyncio.sleep(0)
            if self._handler is not None:
                self._handler(None, bytearray([0x80, opcode, result]))

        asyncio.create_task(_fire())


def _make(
    client: _MockClient,
    bus: EventBus,
    *,
    max_watts: int = 800,
    min_watts: int = 0,
) -> FtmsControl:
    return FtmsControl(
        client,  # type: ignore[arg-type]
        bus,
        device_kind="bike_trainer",
        device_name="MockKICKR",
        max_watts=max_watts,
        min_watts=min_watts,
    )


async def _next_of_type(
    q: asyncio.Queue,
    type_: str,
    *,
    timeout_s: float = 1.0,
) -> object:
    """Read from ``q`` until an envelope of the given ``type_`` arrives.

    Tests that subscribe AFTER ``request_control_and_start`` get the
    replayed ``control_acquired`` event first (it's session-state), then the
    test's own event. Rather than count expected leading events per test,
    skip past whatever's there until the one we care about lands."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"no {type_} event received within {timeout_s}s")
        env = await asyncio.wait_for(q.get(), timeout=remaining)
        if env.type == type_:
            return env


# --- happy path -----------------------------------------------------------


async def test_request_control_and_start_publishes_control_acquired() -> None:
    bus = EventBus()
    client = _MockClient()
    ctrl = _make(client, bus)

    async with bus.subscribe() as q:
        await ctrl.attach()
        ok = await ctrl.request_control_and_start()
        assert ok is True
        assert ctrl.is_controlling is True
        env = await asyncio.wait_for(q.get(), timeout=1.0)

    assert env.type == "control_acquired"
    # Two opcodes were written in order: 0x00 (Request Control), 0x07 (Start).
    assert client.writes[0] == bytes([OP_REQUEST_CONTROL])
    assert client.writes[1] == bytes([OP_START])


async def test_request_control_and_start_logs_explicit_success_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """KICKR LED state is a misleading proxy for "control claimed" — solid
    blue just means BLE GATT connected. The sidecar log should carry an
    explicit success line so operators can verify the actual control claim."""
    import logging

    caplog.set_level(logging.INFO, logger="roguerglike_sidecar.ble.ftms_control")
    bus = EventBus()
    client = _MockClient()
    ctrl = _make(client, bus)
    await ctrl.attach()
    await ctrl.request_control_and_start()

    matching = [
        r
        for r in caplog.records
        if "claimed control" in r.getMessage() and "MockKICKR" in r.getMessage()
    ]
    assert matching, "expected a 'claimed control of <device>' INFO log line on success"


async def test_set_target_power_writes_int16_and_publishes_accepted() -> None:
    bus = EventBus()
    client = _MockClient()
    ctrl = _make(client, bus)
    await ctrl.attach()
    await ctrl.request_control_and_start()

    async with bus.subscribe() as q:
        await ctrl.set_target_power(235)
        env = await _next_of_type(q, "target_power_set")

    assert isinstance(env.data, TargetPowerSetData)
    assert env.data.watts == 235
    assert env.data.accepted is True
    # Last write should be opcode + int16 LE of 235.
    expected = bytes([OP_SET_TARGET_POWER]) + struct.pack("<h", 235)
    assert client.writes[-1] == expected
    assert ctrl.last_target_watts == 235


# --- safety + rejection ---------------------------------------------------


async def test_set_target_power_clamps_above_max() -> None:
    bus = EventBus()
    client = _MockClient()
    ctrl = _make(client, bus, max_watts=400)
    await ctrl.attach()
    await ctrl.request_control_and_start()

    async with bus.subscribe() as q:
        await ctrl.set_target_power(9999)
        env = await _next_of_type(q, "target_power_set")

    assert env.data.watts == 400  # type: ignore[union-attr]
    expected = bytes([OP_SET_TARGET_POWER]) + struct.pack("<h", 400)
    assert client.writes[-1] == expected


async def test_set_target_power_clamps_below_min() -> None:
    bus = EventBus()
    client = _MockClient()
    ctrl = _make(client, bus, min_watts=50)
    await ctrl.attach()
    await ctrl.request_control_and_start()

    async with bus.subscribe() as q:
        await ctrl.set_target_power(10)
        env = await _next_of_type(q, "target_power_set")

    assert env.data.watts == 50  # type: ignore[union-attr]


async def test_set_target_power_without_control_is_rejected() -> None:
    """A command issued before request_control_and_start must not write to the
    trainer; it should publish a rejection envelope so the engine knows."""
    bus = EventBus()
    client = _MockClient()
    ctrl = _make(client, bus)

    async with bus.subscribe() as q:
        await ctrl.set_target_power(200)
        env = await asyncio.wait_for(q.get(), timeout=1.0)

    assert env.type == "target_power_set"
    assert env.data.accepted is False  # type: ignore[union-attr]
    assert "not controlling" in env.data.reason  # type: ignore[union-attr]
    assert client.writes == []  # nothing reached the wire


async def test_request_control_rejected_by_trainer_returns_false() -> None:
    """If Request Control comes back with a non-success result code, we don't
    enter the controlling state and don't proceed to Start."""
    bus = EventBus()
    client = _MockClient(result_overrides={OP_REQUEST_CONTROL: 0x05})  # not permitted
    ctrl = _make(client, bus)
    await ctrl.attach()

    ok = await ctrl.request_control_and_start()
    assert ok is False
    assert ctrl.is_controlling is False
    # Only Request Control was attempted; Start was not.
    assert client.writes == [bytes([OP_REQUEST_CONTROL])]


async def test_set_target_power_trainer_rejection_surfaces_in_reason() -> None:
    bus = EventBus()
    client = _MockClient(result_overrides={OP_SET_TARGET_POWER: 0x03})  # Invalid Parameter
    ctrl = _make(client, bus)
    await ctrl.attach()
    await ctrl.request_control_and_start()

    async with bus.subscribe() as q:
        await ctrl.set_target_power(200)
        env = await _next_of_type(q, "target_power_set")

    assert env.data.accepted is False  # type: ignore[union-attr]
    assert "0x03" in env.data.reason  # type: ignore[union-attr]
    # The clamped value is what we sent to the trainer; it's surfaced even
    # though the trainer rejected it, so logs can show what was tried.
    assert env.data.watts == 200  # type: ignore[union-attr]


# --- lifecycle -----------------------------------------------------------


async def test_stop_releases_controlling_state() -> None:
    bus = EventBus()
    client = _MockClient()
    ctrl = _make(client, bus)
    await ctrl.attach()
    await ctrl.request_control_and_start()
    assert ctrl.is_controlling

    async with bus.subscribe() as q:
        await ctrl.stop()
        env = await _next_of_type(q, "control_released")

    assert ctrl.is_controlling is False
    assert env.data.reason == "stop"  # type: ignore[union-attr]


async def test_release_is_idempotent() -> None:
    """release() can be called even when not controlling (e.g. on disconnect
    after never claiming) without raising."""
    bus = EventBus()
    client = _MockClient()
    ctrl = _make(client, bus)
    await ctrl.attach()

    async with bus.subscribe() as q:
        await ctrl.release("test")
        env = await asyncio.wait_for(q.get(), timeout=1.0)

    assert env.type == "control_released"
    assert env.data.reason == "test"  # type: ignore[union-attr]


async def test_reconnect_restores_last_target_watts() -> None:
    """After a successful set_target_power, a second request_control_and_start
    (simulating reconnect) replays the last target so the trainer ends up where
    the game left it."""
    bus = EventBus()
    client = _MockClient()
    ctrl = _make(client, bus)
    await ctrl.attach()
    await ctrl.request_control_and_start()
    await ctrl.set_target_power(300)
    assert ctrl.last_target_watts == 300

    # Reset is_controlling to simulate a drop, then re-attach.
    ctrl._is_controlling = False  # noqa: SLF001 — simulating reconnect for the test
    client.writes.clear()
    ok = await ctrl.request_control_and_start()
    assert ok is True

    # After the claim sequence, the restore Set Target Power should be present.
    set_power_writes = [w for w in client.writes if w[0] == OP_SET_TARGET_POWER]
    assert len(set_power_writes) == 1
    assert set_power_writes[0] == bytes([OP_SET_TARGET_POWER]) + struct.pack("<h", 300)


# --- protocol details ----------------------------------------------------


async def test_stop_writes_opcode_plus_subparameter() -> None:
    """FTMS Stop opcode (0x08) requires a 1-byte subparameter: 0x01 = stop,
    0x02 = pause. We always send stop."""
    bus = EventBus()
    client = _MockClient()
    ctrl = _make(client, bus)
    await ctrl.attach()
    await ctrl.request_control_and_start()
    client.writes.clear()
    await ctrl.stop()
    assert client.writes[0] == bytes([OP_STOP, 0x01])


async def test_indication_with_wrong_response_opcode_is_ignored() -> None:
    """The handler must only set the future on a true response indication
    (byte 0 == 0x80). Notifications without the response prefix don't unblock
    the awaiter — they'd be unrelated FTMS events that share the characteristic.
    """
    bus = EventBus()
    client = _MockClient(silent_opcodes={OP_REQUEST_CONTROL})
    ctrl = _make(client, bus, max_watts=800)
    await ctrl.attach()

    # Fire a fake non-response notification before the real timeout fires.
    if client._handler is not None:  # noqa: SLF001
        # No effect — handler should ignore it
        client._handler(None, bytearray([0x99, OP_REQUEST_CONTROL, 0x01]))  # noqa: SLF001

    with pytest.raises(TimeoutError):
        # Force a faster timeout for the test by patching the constant.
        import roguerglike_sidecar.ble.ftms_control as mod

        original = mod.INDICATION_TIMEOUT_S
        mod.INDICATION_TIMEOUT_S = 0.1
        try:
            # Request control silently — no indication will fire — so the
            # opcode send times out internally and returns False rather than
            # raising. To assert the timeout path we bypass the catch in
            # request_control_and_start and probe _send_opcode directly.
            await ctrl._send_opcode(OP_REQUEST_CONTROL)  # noqa: SLF001
        finally:
            mod.INDICATION_TIMEOUT_S = original
