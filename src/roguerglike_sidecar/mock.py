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
    ControlAcquiredData,
    ControlReleasedData,
    DeviceCapabilitiesData,
    DeviceConnectedData,
    DeviceKind,
    HeartRateData,
    PausedData,
    PowerData,
    ResumedData,
    SimulationSetData,
    TargetPowerSetData,
)
from .session import announce_session_start
from .ws_server import EventBus

log = logging.getLogger(__name__)

DEVICE_KIND: DeviceKind = "mock"
DEVICE_NAME = "Mock Bike"
TICK_HZ = 1.0


@dataclass
class MockState:
    """Live slider values + (optionally) an ERG target that overrides them.

    Slider values are mutated by the web UI and read by the producer loop.
    When ``erg_target_watts`` is set (via :meth:`set_erg_target_power`), the
    producer emits *that* value as the published power event instead of the
    slider — simulating what a real trainer would do under an FTMS Set
    Target Power command. Setting it to ``None`` (via :meth:`stop_erg`)
    returns control of the published watts to the slider.
    """

    watts: int = 0
    rpm: int = 0
    bpm: int = 60
    erg_target_watts: int | None = None
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

    async def set_erg_target_power(self, watts: int) -> int:
        """Activate or update mock ERG mode. Returns the clamped value the
        mock will actually publish."""
        async with self._lock:
            clamped = max(0, min(3000, int(watts)))
            self.erg_target_watts = clamped
            return clamped

    async def stop_erg(self) -> None:
        """Return the mock to slider-driven power."""
        async with self._lock:
            self.erg_target_watts = None

    async def snapshot(self) -> tuple[int, int, int]:
        """Returns (effective_watts, rpm, bpm). ``effective_watts`` is the
        ERG target if active, otherwise the current slider value."""
        async with self._lock:
            target = self.erg_target_watts
            effective_watts = target if target is not None else self.watts
            return effective_watts, self.rpm, self.bpm


async def announce_device(bus: EventBus) -> None:
    """One-shot at startup: session_start + device_connected + capabilities.

    The session_start helper is shared with live mode (see ``session.py``);
    device_connected and device_capabilities are mock-specific (live mode
    emits them from BleSource). Mock advertises target_power=True so engine
    code that gates ERG UI on the capability can be exercised off-bike.
    """
    await announce_session_start(bus, DEVICE_KIND)
    await bus.publish(
        type_="device_connected",
        data=DeviceConnectedData(kind=DEVICE_KIND, name=DEVICE_NAME),
        device_kind=DEVICE_KIND,
    )
    await bus.publish(
        type_="device_capabilities",
        data=DeviceCapabilitiesData(kind=DEVICE_KIND, name=DEVICE_NAME, target_power=True),
        device_kind=DEVICE_KIND,
    )


async def mock_acquire_control(bus: EventBus) -> None:
    """Mock equivalent of FtmsControl.request_control_and_start. Emits
    ``control_acquired`` so engine code can move past its waiting state."""
    await bus.publish(
        type_="control_acquired",
        data=ControlAcquiredData(kind=DEVICE_KIND, name=DEVICE_NAME),
        device_kind=DEVICE_KIND,
    )


async def mock_release_control(bus: EventBus, reason: str = "released") -> None:
    """Mock equivalent of FtmsControl.release."""
    await bus.publish(
        type_="control_released",
        data=ControlReleasedData(kind=DEVICE_KIND, name=DEVICE_NAME, reason=reason),
        device_kind=DEVICE_KIND,
    )


async def mock_set_target_power(bus: EventBus, state: MockState, watts: int) -> None:
    """Apply an ERG target to the mock and publish the canonical
    ``target_power_set`` envelope. Mirrors :meth:`FtmsControl.set_target_power`
    so engine code is wire-equivalent across mock + live modes."""
    applied = await state.set_erg_target_power(watts)
    await bus.publish(
        type_="target_power_set",
        data=TargetPowerSetData(watts=applied, accepted=True),
        device_kind=DEVICE_KIND,
    )


async def mock_structured_pause(
    bus: EventBus,
    state: MockState,
    *,
    easy_spin_watts: int,
    previous_target_watts: int,
    reason: str | None,
) -> None:
    """Apply a structured pause in mock mode. Sets the ERG target to the
    easy-spin wattage and publishes the canonical ``paused`` envelope.

    Mock has no cadence bailout, so the bookkeeping (previous target
    tracking, idempotency) lives in the cli.py closure. This helper just
    handles the state write + envelope publish."""
    clamped = await state.set_erg_target_power(easy_spin_watts)
    await bus.publish(
        type_="paused",
        data=PausedData(
            kind=DEVICE_KIND,
            name=DEVICE_NAME,
            reason=reason or "",
            target_watts=clamped,
            previous_target_watts=previous_target_watts,
        ),
        device_kind=DEVICE_KIND,
    )


async def mock_set_simulation(
    bus: EventBus,
    *,
    grade_percent: float,
    wind_speed_mps: float,
    rolling_resistance: float,
    wind_resistance: float,
) -> None:
    """Mock equivalent of FtmsControl.set_simulation. Always accepts (mock
    has no physics model behind it; the envelope is the observable).
    Useful for engine/client dev of SIM-mode UI without hardware."""
    await bus.publish(
        type_="simulation_set",
        data=SimulationSetData(
            grade_percent=grade_percent,
            wind_speed_mps=wind_speed_mps,
            rolling_resistance=rolling_resistance,
            wind_resistance=wind_resistance,
            accepted=True,
        ),
        device_kind=DEVICE_KIND,
    )


async def mock_structured_resume(
    bus: EventBus,
    state: MockState,
    *,
    restored_to_watts: int,
) -> None:
    """Apply a structured resume in mock mode. Restores the ERG target
    immediately (no ramp — mock doesn't model intensity-aware ramping;
    the ramp duration on the envelope is reported as 0.0)."""
    clamped = await state.set_erg_target_power(restored_to_watts)
    await bus.publish(
        type_="resumed",
        data=ResumedData(
            kind=DEVICE_KIND,
            name=DEVICE_NAME,
            restored_to_watts=clamped,
            ramped_over_s=0.0,
        ),
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
