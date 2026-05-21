"""Mock event producer.

Holds the current slider state (power / cadence / heart rate) and ticks an async
loop that publishes the corresponding events to the bus. The slider UI mutates
``MockState`` via ``set_*`` methods; this module only reads it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .events import (
    CadenceData,
    DeviceConnectedData,
    DeviceKind,
    HeartRateData,
    PowerData,
    SessionStartData,
)
from .ws_server import EventBus

log = logging.getLogger(__name__)

DEVICE_KIND: DeviceKind = "mock"
DEVICE_NAME = "Mock Bike"
TICK_HZ = 1.0


@dataclass
class MockState:
    """Live slider values. Mutated by the UI, read by the producer loop."""

    watts: int = 0
    rpm: int = 0
    bpm: int = 60
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def set_watts(self, v: int) -> None:
        async with self._lock:
            self.watts = max(0, min(3000, int(v)))

    async def set_rpm(self, v: int) -> None:
        async with self._lock:
            self.rpm = max(0, min(250, int(v)))

    async def set_bpm(self, v: int) -> None:
        async with self._lock:
            self.bpm = max(20, min(240, int(v)))

    async def snapshot(self) -> tuple[int, int, int]:
        async with self._lock:
            return self.watts, self.rpm, self.bpm


async def announce_device(bus: EventBus) -> None:
    """One-shot at startup: session_start + device_connected."""
    await bus.publish(
        type_="session_start",
        data=SessionStartData(session_id=bus.session_id),
        device_kind=DEVICE_KIND,
    )
    await bus.publish(
        type_="device_connected",
        data=DeviceConnectedData(kind=DEVICE_KIND, name=DEVICE_NAME),
        device_kind=DEVICE_KIND,
    )


async def run_mock_loop(bus: EventBus, state: MockState, *, hz: float = TICK_HZ) -> None:
    """Publish power/cadence/heart_rate at the given rate until cancelled."""
    period = 1.0 / hz
    await announce_device(bus)
    log.info("mock producer running at %.1f Hz", hz)
    while True:
        watts, rpm, bpm = await state.snapshot()
        await bus.publish(
            type_="power",
            data=PowerData(watts=watts),
            device_kind=DEVICE_KIND,
        )
        await bus.publish(
            type_="cadence",
            data=CadenceData(rpm=rpm),
            device_kind=DEVICE_KIND,
        )
        await bus.publish(
            type_="heart_rate",
            data=HeartRateData(bpm=bpm),
            device_kind=DEVICE_KIND,
        )
        await asyncio.sleep(period)
