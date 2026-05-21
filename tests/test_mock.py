"""Mock producer: state clamping + producer loop emits expected event types."""

from __future__ import annotations

import asyncio

import pytest

from roguerglike_sidecar.mock import MockState, run_mock_loop
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


async def test_mock_loop_emits_power_cadence_heart_rate_per_tick() -> None:
    bus = EventBus()
    state = MockState()
    await state.set_watts(180)
    await state.set_rpm(85)
    await state.set_bpm(135)

    async with bus.subscribe() as q:
        loop_task = asyncio.create_task(run_mock_loop(bus, state, hz=50.0))
        try:
            seen: list[str] = []
            # First two events are session_start + device_connected; then per-tick triples.
            for _ in range(5):
                env = await asyncio.wait_for(q.get(), timeout=1.0)
                seen.append(env.type)
        finally:
            loop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop_task

    assert seen[:2] == ["session_start", "device_connected"]
    assert set(seen[2:5]) == {"power", "cadence", "heart_rate"}
