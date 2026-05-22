"""Mock producer: state clamping + producer loop emits expected event types."""

from __future__ import annotations

import asyncio

import pytest

from roguerglike_sidecar.mock import (
    MockState,
    mock_acquire_control,
    mock_release_control,
    mock_set_target_power,
    run_mock_loop,
)
from roguerglike_sidecar.ws_server import EventBus


async def test_mock_state_clamps_to_valid_range() -> None:
    state = MockState()
    await state.set_watts(-100)
    await state.set_rpm(9999)
    await state.set_bpm(10)
    w, r, b = await state.snapshot()
    assert w == 0
    assert r == 250
    assert b == 20


async def test_mock_loop_emits_session_then_per_tick_triples() -> None:
    """Startup order: session_start → device_connected → device_capabilities,
    then per-tick (power, cadence, heart_rate) on a loop."""
    bus = EventBus()
    state = MockState()
    await state.set_watts(180)
    await state.set_rpm(85)
    await state.set_bpm(135)

    async with bus.subscribe() as q:
        loop_task = asyncio.create_task(run_mock_loop(bus, state, hz=50.0))
        try:
            seen: list[str] = []
            for _ in range(6):
                env = await asyncio.wait_for(q.get(), timeout=1.0)
                seen.append(env.type)
        finally:
            loop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop_task

    assert seen[:3] == ["session_start", "device_connected", "device_capabilities"]
    assert set(seen[3:6]) == {"power", "cadence", "heart_rate"}


async def test_mock_capabilities_advertise_target_power() -> None:
    """Engine code that gates ERG UI on the device_capabilities event should
    be able to exercise that path against mock mode."""
    bus = EventBus()
    state = MockState()
    async with bus.subscribe() as q:
        loop_task = asyncio.create_task(run_mock_loop(bus, state, hz=50.0))
        try:
            # session_start, device_connected, then device_capabilities.
            for _ in range(2):
                await asyncio.wait_for(q.get(), timeout=1.0)
            caps_env = await asyncio.wait_for(q.get(), timeout=1.0)
        finally:
            loop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop_task
    assert caps_env.type == "device_capabilities"
    assert caps_env.data.target_power is True  # type: ignore[union-attr]


async def test_mock_erg_target_overrides_slider() -> None:
    """When an ERG target is active, MockState.snapshot returns the target
    instead of the slider value. Slider is unchanged so disabling ERG
    restores it."""
    state = MockState()
    await state.set_watts(120)  # rider's "free spin" output
    w, _, _ = await state.snapshot()
    assert w == 120

    applied = await state.set_erg_target_power(250)
    assert applied == 250
    w, _, _ = await state.snapshot()
    assert w == 250  # ERG target wins

    await state.stop_erg()
    w, _, _ = await state.snapshot()
    assert w == 120  # back to slider


async def test_mock_erg_target_clamps_negative_and_huge() -> None:
    state = MockState()
    assert await state.set_erg_target_power(-50) == 0
    assert await state.set_erg_target_power(99_999) == 3000


async def test_mock_set_target_power_publishes_target_power_set() -> None:
    bus = EventBus()
    state = MockState()
    async with bus.subscribe() as q:
        await mock_set_target_power(bus, state, 235)
        env = await asyncio.wait_for(q.get(), timeout=1.0)
    assert env.type == "target_power_set"
    assert env.data.watts == 235  # type: ignore[union-attr]
    assert env.data.accepted is True  # type: ignore[union-attr]


async def test_mock_control_lifecycle_acquire_then_release() -> None:
    bus = EventBus()
    async with bus.subscribe() as q:
        await mock_acquire_control(bus)
        env = await asyncio.wait_for(q.get(), timeout=1.0)
        assert env.type == "control_acquired"
        await mock_release_control(bus, "test")
        env = await asyncio.wait_for(q.get(), timeout=1.0)
        assert env.type == "control_released"
        assert env.data.reason == "test"  # type: ignore[union-attr]
