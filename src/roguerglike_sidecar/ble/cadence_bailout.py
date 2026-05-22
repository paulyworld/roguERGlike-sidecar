"""Cadence-driven safety bailout for ERG control.

The problem this solves: in ERG mode the trainer holds a target wattage
regardless of pedal motion. If the rider stops pedalling at a high target
(say 300 W) and later tries to restart, the cranks are locked solid against
that resistance — uncomfortable at best, impossible at worst. This module
watches cadence and, when the rider has been off the bike long enough, drops
the target to the trainer's configured minimum so the cranks free up. When
cadence resumes it ramps the target back to where the game left it.

Thresholds are **intensity-aware**: per the project memory
``intensity-aware-safety-curves``, the right wait-before-bailout and the
right resume-ramp duration both depend on current % FTP.

  - At low intensity (~50% FTP recovery): patient bailout (90 s default) +
    fast ramp (2 s) — a casual water-bottle break shouldn't trigger; restart
    is easy.
  - At high intensity (~150% FTP burst): fast bailout (15 s) + slow ramp
    (12 s) — if cadence drops at threshold, the rider is done; ramping back
    to that target needs care to avoid an immediate re-stop.

The curve is linear in ``pct_ftp ∈ [0.5, 1.5]``, clamped outside. Constants
are in code (not flags) for the first iteration — promote to CLI flags only
if real use shows they need tuning.

Without a configured ``rider_ftp``, falls back to static
``static_bailout_s`` and ``static_ramp_s`` values for all intensities.

The bailout owns the command path while it's paused: if the engine sends
``set_target_power`` during the pause, the bailout records the new target as
the *intended* restore value, but doesn't write to the trainer until
cadence resumes. The engine sees a ``target_power_set`` ack with
``reason="bailout-pending"`` so it can distinguish "queued" from "applied".
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from ..events import (
    CadenceBailoutDisengagedData,
    CadenceBailoutEngagedData,
    CadenceData,
    DeviceKind,
    TargetPowerSetData,
)
from ..ws_server import EventBus

if TYPE_CHECKING:
    from .ftms_control import FtmsControl

log = logging.getLogger(__name__)

# Cadence at or above this is considered "rider active on the bike". Tuned
# to be well above sensor noise but well below any meaningful pedal cadence.
ACTIVE_CADENCE_RPM = 30

# Linear-curve anchor intensities (target / rider_ftp). At pct_ftp <= LOW the
# bailout is patient and the ramp is fast; at pct_ftp >= HIGH the bailout is
# fast and the ramp is slow.
PCT_FTP_LOW = 0.5
PCT_FTP_HIGH = 1.5

# Bailout window (seconds before pause engages, given sustained low cadence).
BAILOUT_S_AT_LOW_PCT = 90.0
BAILOUT_S_AT_HIGH_PCT = 15.0

# Resume ramp duration (seconds to lerp from min target back to pre-pause).
RAMP_S_AT_LOW_PCT = 2.0
RAMP_S_AT_HIGH_PCT = 12.0

# Ramp granularity: emit a set_target_power roughly this often during the
# resume ramp. ~2 Hz keeps each step ~50 W or less on the steepest profile.
RAMP_TICK_S = 0.5

# Internal watcher poll rate.
WATCHER_TICK_S = 1.0


def _lerp_clamped(low: float, high: float, t: float) -> float:
    if t <= 0.0:
        return low
    if t >= 1.0:
        return high
    return low + (high - low) * t


def _interp_for_pct_ftp(low_value: float, high_value: float, pct_ftp: float) -> float:
    """Linearly interpolate between ``low_value`` (at PCT_FTP_LOW or below)
    and ``high_value`` (at PCT_FTP_HIGH or above), based on ``pct_ftp``."""
    t = (pct_ftp - PCT_FTP_LOW) / (PCT_FTP_HIGH - PCT_FTP_LOW)
    return _lerp_clamped(low_value, high_value, t)


class CadenceBailout:
    """Watch cadence; pause ERG when rider stops; ramp back on resume.

    Wraps an ``FtmsControl``. ``handle_set_target_power`` is the entry point
    the CLI command handler should call instead of ``control.set_target_power``
    directly, so that engine commands during a paused window are stored
    rather than written immediately.
    """

    def __init__(
        self,
        control: FtmsControl,
        bus: EventBus,
        *,
        device_kind: DeviceKind,
        device_name: str,
        rider_ftp: int | None = None,
        static_bailout_s: float = 60.0,
        static_ramp_s: float = 3.0,
    ) -> None:
        self._control = control
        self._bus = bus
        self._device_kind = device_kind
        self._device_name = device_name
        self._rider_ftp = rider_ftp if (rider_ftp and rider_ftp > 0) else None
        self._static_bailout_s = static_bailout_s
        self._static_ramp_s = static_ramp_s

        self._paused = False
        self._pre_pause_target: int | None = None
        self._last_active_ts = time.monotonic()
        self._ramp_task: asyncio.Task[None] | None = None

    # --- public state for tests / logging ----------------------------

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def pre_pause_target_watts(self) -> int | None:
        return self._pre_pause_target

    # --- intensity-aware curves --------------------------------------

    def _current_pct_ftp(self, watts: int | None) -> float | None:
        if self._rider_ftp is None or watts is None or watts <= 0:
            return None
        return watts / self._rider_ftp

    def _bailout_s_for(self, watts: int | None) -> float:
        pct = self._current_pct_ftp(watts)
        if pct is None:
            return self._static_bailout_s
        return _interp_for_pct_ftp(BAILOUT_S_AT_LOW_PCT, BAILOUT_S_AT_HIGH_PCT, pct)

    def _ramp_s_for(self, watts: int | None) -> float:
        pct = self._current_pct_ftp(watts)
        if pct is None:
            return self._static_ramp_s
        return _interp_for_pct_ftp(RAMP_S_AT_LOW_PCT, RAMP_S_AT_HIGH_PCT, pct)

    # --- command path ------------------------------------------------

    async def handle_set_target_power(self, watts: int) -> None:
        """Route an engine-requested target through the bailout.

        While paused, the command is queued: ``pre_pause_target`` is updated
        so the eventual resume goes to the new value, but nothing is written
        to the trainer. The engine receives a ``target_power_set`` ack
        with ``reason="bailout-pending"`` so it can distinguish queued from
        applied. While active, passes straight through to the FtmsControl.

        A non-floor target also resets the idle timer. The bailout's
        semantic is "rider has been off the bike at a meaningful target for
        N seconds" — so the clock starts ticking from the first meaningful
        target, not from the bailout's own construction time. Without this,
        an operator who launches the sidecar and waits >N seconds before
        pressing Start Workout would have the bailout fire immediately on
        the engine's first ``set_target_power``.
        """
        if self._paused:
            self._pre_pause_target = int(watts)
            await self._bus.publish(
                type_="target_power_set",
                data=TargetPowerSetData(
                    watts=int(watts),
                    accepted=False,
                    reason="bailout-pending",
                ),
                device_kind=self._device_kind,
            )
            return
        if watts > self._control.min_target_watts:
            self._last_active_ts = time.monotonic()
        await self._control.set_target_power(watts)

    # --- bus subscription + watcher ----------------------------------

    async def run(self) -> None:
        """Subscribe to the bus's cadence stream and run the idle watcher in
        parallel until cancelled. Both tasks share state on ``self``."""
        async with self._bus.subscribe() as q:
            watcher = asyncio.create_task(self._watcher_loop())
            try:
                while True:
                    env = await q.get()
                    if env.type == "cadence" and isinstance(env.data, CadenceData):
                        await self._on_cadence(int(env.data.rpm))
            finally:
                watcher.cancel()
                if self._ramp_task is not None:
                    self._ramp_task.cancel()

    async def _on_cadence(self, rpm: int) -> None:
        if rpm >= ACTIVE_CADENCE_RPM:
            self._last_active_ts = time.monotonic()
            if self._paused:
                # Rider's back. Exit the paused state and start a fresh ramp.
                # If a ramp is somehow still in flight, cancel it and restart
                # — defensive; shouldn't normally happen.
                if self._ramp_task is not None and not self._ramp_task.done():
                    self._ramp_task.cancel()
                self._paused = False
                self._ramp_task = asyncio.create_task(self._do_resume_ramp())

    async def _watcher_loop(self) -> None:
        while True:
            await asyncio.sleep(WATCHER_TICK_S)
            if self._paused or not self._control.is_controlling:
                continue
            target = self._control.last_target_watts
            if target is None or target <= self._control.min_target_watts:
                # Nothing to bail out from — trainer is already at floor.
                continue
            idle_s = time.monotonic() - self._last_active_ts
            if idle_s >= self._bailout_s_for(target):
                await self._engage_pause()

    # --- pause / resume ----------------------------------------------

    async def _engage_pause(self) -> None:
        bailout_s = self._bailout_s_for(self._control.last_target_watts)
        self._pre_pause_target = self._control.last_target_watts
        self._paused = True
        log.warning(
            "%s: cadence bailout engaged after %.0fs idle; dropping target from %dW to %dW",
            self._device_name,
            bailout_s,
            self._pre_pause_target or 0,
            self._control.min_target_watts,
        )
        # Drop to floor. last_target_watts still tracks this write, which is
        # what we want — if the engine reconnects mid-pause, reconnect-restore
        # would replay the floor, not the pre-pause value. Resume ramp will
        # take it back up.
        await self._control.set_target_power(self._control.min_target_watts)
        await self._bus.publish(
            type_="cadence_bailout_engaged",
            data=CadenceBailoutEngagedData(
                kind=self._device_kind,
                name=self._device_name,
                pre_pause_target_watts=self._pre_pause_target or 0,
                bailout_after_s=bailout_s,
            ),
            device_kind=self._device_kind,
        )

    async def _do_resume_ramp(self) -> None:
        """Interpolate target from current (min) up to ``pre_pause_target``
        over the intensity-appropriate ramp duration. Steps fire roughly at
        ``RAMP_TICK_S``. If the rider stops again mid-ramp, the task is
        cancelled by ``_on_cadence``'s re-pause logic."""
        if self._pre_pause_target is None:
            return  # nothing to restore to
        end_watts = int(self._pre_pause_target)
        ramp_s = self._ramp_s_for(end_watts)
        start_watts = self._control.last_target_watts or self._control.min_target_watts
        log.info(
            "%s: cadence bailout disengaging; ramping %dW → %dW over %.1fs",
            self._device_name,
            start_watts,
            end_watts,
            ramp_s,
        )

        if ramp_s <= 0.0 or end_watts == start_watts:
            await self._control.set_target_power(end_watts)
        else:
            steps = max(1, int(round(ramp_s / RAMP_TICK_S)))
            try:
                for i in range(1, steps + 1):
                    t = i / steps
                    watts = int(round(start_watts + (end_watts - start_watts) * t))
                    await self._control.set_target_power(watts)
                    if i < steps:
                        await asyncio.sleep(ramp_s / steps)
            except asyncio.CancelledError:
                log.info(
                    "%s: cadence bailout resume ramp cancelled mid-ramp",
                    self._device_name,
                )
                raise

        await self._bus.publish(
            type_="cadence_bailout_disengaged",
            data=CadenceBailoutDisengagedData(
                kind=self._device_kind,
                name=self._device_name,
                restored_to_watts=end_watts,
                ramped_over_s=ramp_s,
            ),
            device_kind=self._device_kind,
        )
