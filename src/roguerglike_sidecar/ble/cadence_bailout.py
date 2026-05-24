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
from typing import TYPE_CHECKING, Literal

from ..events import (
    CadenceBailoutDisengagedData,
    CadenceBailoutEngagedData,
    CadenceData,
    DeviceKind,
    PausedData,
    ResumedData,
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

        # ``_paused`` means *either* cadence-paused or structured-paused.
        # The watcher / command-handler skip-paths use this. The two
        # secondary flags distinguish which kind, since they have different
        # resume semantics (cadence resumes on pedalling return; structured
        # only on explicit resume command).
        self._paused = False
        self._structured_paused = False
        self._structured_pause_reason: str | None = None

        self._pre_pause_target: int | None = None
        self._last_active_ts = time.monotonic()
        self._ramp_task: asyncio.Task[None] | None = None

    # --- public state for tests / logging ----------------------------

    @property
    def is_paused(self) -> bool:
        """True if either cadence-bailout OR structured-pause is active."""
        return self._paused

    @property
    def is_structured_paused(self) -> bool:
        return self._structured_paused

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
            # Distinguish reasons so clients can tell why their write was
            # deferred. Structured pause is an intentional client-side
            # state; cadence bailout is a safety state. UI may want to
            # surface them differently.
            reason = "deferred-paused" if self._structured_paused else "bailout-pending"
            await self._bus.publish(
                type_="target_power_set",
                data=TargetPowerSetData(
                    watts=int(watts),
                    accepted=False,
                    reason=reason,
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
            if self._paused and not self._structured_paused:
                # Cadence-paused and rider's back → exit the paused state
                # and start a fresh ramp. If a ramp is somehow still in
                # flight, cancel it and restart — defensive; shouldn't
                # normally happen. Skipped when structured-paused: a
                # client-driven pause is explicit; only ``resume`` can
                # exit it, not the rider picking pedalling back up.
                if self._ramp_task is not None and not self._ramp_task.done():
                    self._ramp_task.cancel()
                self._paused = False
                self._ramp_task = asyncio.create_task(self._do_resume_ramp(source="cadence"))

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

    async def _do_resume_ramp(self, *, source: Literal["cadence", "structured"]) -> None:
        """Interpolate target from current (min/easy-spin) up to
        ``pre_pause_target`` over the intensity-appropriate ramp duration.
        Steps fire roughly at ``RAMP_TICK_S``. If the rider stops again
        mid-ramp, the task is cancelled by ``_on_cadence``'s re-pause logic.

        ``source`` controls which completion envelope fires —
        ``cadence_bailout_disengaged`` (safety pause cleared by pedalling)
        or ``resumed`` (structured pause cleared by ``resume`` command).
        The ramp mechanics are identical; only the published event differs."""
        if self._pre_pause_target is None:
            return  # nothing to restore to
        end_watts = int(self._pre_pause_target)
        ramp_s = self._ramp_s_for(end_watts)
        start_watts = self._control.last_target_watts or self._control.min_target_watts
        log.info(
            "%s: %s resume; ramping %dW → %dW over %.1fs",
            self._device_name,
            "cadence bailout" if source == "cadence" else "structured pause",
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
                    "%s: %s resume ramp cancelled mid-ramp",
                    self._device_name,
                    "cadence bailout" if source == "cadence" else "structured pause",
                )
                raise

        if source == "structured":
            await self._bus.publish(
                type_="resumed",
                data=ResumedData(
                    kind=self._device_kind,
                    name=self._device_name,
                    restored_to_watts=end_watts,
                    ramped_over_s=ramp_s,
                ),
                device_kind=self._device_kind,
            )
        else:
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

    # --- structured pause (Pattern B) --------------------------------

    async def structured_pause(
        self,
        *,
        easy_spin_watts: int,
        reason: str | None = None,
    ) -> bool:
        """Engage a client-driven structured pause. Returns ``True`` if the
        state transitioned, ``False`` if already structured-paused
        (idempotent no-op — ``target_watts`` *is* still applied so a
        client can update the easy-spin without flipping state, but no
        ``paused`` envelope re-fires).

        While structured-paused: cadence-bailout watching is suspended
        (the watcher checks ``self._paused`` and skips), inbound
        ``set_target_power`` is queued + acked with
        ``reason="deferred-paused"``, and ``_on_cadence`` no longer
        auto-resumes on pedalling return. Only ``structured_resume``
        clears the state."""
        clamped = max(self._control.min_target_watts, easy_spin_watts)
        clamped = min(self._control.max_target_watts, clamped)

        if self._structured_paused:
            # Idempotent: apply the updated easy-spin but don't re-publish
            # the paused envelope. Useful when a client adjusts the
            # easy-spin mid-pause (e.g. dropping further during a long
            # break).
            await self._control.set_target_power(clamped)
            return False

        # Capture pre-pause if we don't already have one from a prior
        # cadence-pause. (If the cadence bailout fired first and then a
        # structured pause arrives, keep the cadence-pause's captured
        # target — it's the rider's last meaningful intent.)
        if self._pre_pause_target is None:
            self._pre_pause_target = self._control.last_target_watts

        # If a cadence-resume ramp was in flight (rider just came back
        # but the structured pause says "no, stay down"), cancel it.
        if self._ramp_task is not None and not self._ramp_task.done():
            self._ramp_task.cancel()

        self._paused = True
        self._structured_paused = True
        self._structured_pause_reason = reason

        log.info(
            "%s: structured pause engaged (reason=%r, easy_spin=%dW, previous=%dW)",
            self._device_name,
            reason or "",
            clamped,
            self._pre_pause_target or 0,
        )

        await self._control.set_target_power(clamped)
        await self._bus.publish(
            type_="paused",
            data=PausedData(
                kind=self._device_kind,
                name=self._device_name,
                reason=reason or "",
                target_watts=clamped,
                previous_target_watts=self._pre_pause_target or 0,
            ),
            device_kind=self._device_kind,
        )
        return True

    async def structured_resume(self) -> bool:
        """Clear a structured pause and ramp back to the (possibly
        updated) pre-pause target. Returns ``True`` if the state
        transitioned, ``False`` if not structured-paused (idempotent
        no-op).

        If the cadence-bailout timer has aged past the threshold while
        the structured pause was active, the watcher will re-engage on
        its next tick — that's correct behavior, the rider has actually
        been off the bike."""
        if not self._structured_paused:
            return False

        log.info(
            "%s: structured pause cleared (reason=%r); initiating resume ramp",
            self._device_name,
            self._structured_pause_reason or "",
        )

        self._structured_paused = False
        self._structured_pause_reason = None
        self._paused = False
        # Reset the bailout idle timer so the cadence watcher doesn't
        # immediately re-engage just because the rider was structurally
        # paused for longer than the bailout window. Real absence is
        # measured from *now* forward.
        self._last_active_ts = time.monotonic()

        if self._ramp_task is not None and not self._ramp_task.done():
            self._ramp_task.cancel()
        self._ramp_task = asyncio.create_task(self._do_resume_ramp(source="structured"))
        return True
