"""Entry point: ``roguerglike-sidecar --mode {mock|live}`` plus ``--scan``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from typing import TYPE_CHECKING

import click

from .ble.ftms_bike import FtmsBikeProfile
from .ble.ftms_control import FtmsControl
from .ble.hrs import HrsProfile
from .ble.profile import BleProfile
from .ble.scan import DiscoveredDevice, scan_for_profiles
from .ble.source import BleSource

if TYPE_CHECKING:
    from bleak import BleakClient
from .events import (
    Command,
    EventType,
    ReleaseControlCommand,
    SetTargetPowerCommand,
    StartCommand,
    StopCommand,
    TargetPowerSetData,
)
from .mock import (
    MockState,
    mock_acquire_control,
    mock_release_control,
    mock_set_target_power,
    run_mock_loop,
)
from .session import announce_session_start
from .web_ui import DEFAULT_PORT as UI_PORT
from .web_ui import run_web_ui
from .ws_server import DEFAULT_PORT as WS_PORT
from .ws_server import EventBus, run_ws_server

log = logging.getLogger(__name__)

# Order matters for output and for the order ``--scan`` lists profiles.
ALL_PROFILES: list[BleProfile] = [FtmsBikeProfile(), HrsProfile()]

# Aggressive defaults per memory ``trainer-control-aggressive-defaults``:
# permissive caps + free spin allowed; safety is opt-in via these flags
# rather than opt-out padding. The disconnect-bailout grace window stays on
# even with aggressive defaults — that's a don't-burn-down-the-trainer
# concern, not a feel concern.
DEFAULT_MAX_TARGET_POWER = 800
DEFAULT_MIN_TARGET_POWER = 0
DEFAULT_DISCONNECT_BAILOUT_S = 10.0


def _match_device(devices: list[DiscoveredDevice], query: str) -> DiscoveredDevice | None:
    """Resolve a user-supplied name-or-address to a discovered device."""
    needle = query.strip().lower()
    for d in devices:
        if d.address.lower() == needle or d.name.lower() == needle:
            return d
    # fall back to substring on name (Wahoo KICKR vs "KICKR CORE 8B2A", etc.)
    for d in devices:
        if needle and needle in d.name.lower():
            return d
    return None


def _resolve_drop_types(
    *,
    has_bike: bool,
    has_hr: bool,
    prefer_bike_hr: bool,
) -> tuple[frozenset[EventType], frozenset[EventType]]:
    """Decide which event types each source should suppress.

    Returns ``(bike_drops, hr_drops)``. The HR-source-of-truth rule: when both
    a bike (which may embed HR in its FTMS packet) and a standalone HR sensor
    are paired, only one should publish ``heart_rate`` events; the other drops
    them. Default prefers the standalone sensor (more accurate); the
    ``--prefer-bike-hr`` flag inverts this.
    """
    if has_bike and has_hr:
        if prefer_bike_hr:
            return frozenset(), frozenset({"heart_rate"})
        return frozenset({"heart_rate"}), frozenset()
    return frozenset(), frozenset()


async def _run_mock(
    ws_port: int,
    ui_port: int,
    *,
    allow_trainer_control: bool,
) -> None:
    bus = EventBus()
    state = MockState()

    async def on_command(command: Command) -> None:
        if isinstance(command, SetTargetPowerCommand):
            await mock_set_target_power(bus, state, command.watts)
        elif isinstance(command, StopCommand):
            await state.stop_erg()
            await mock_release_control(bus, "stop")
        elif isinstance(command, ReleaseControlCommand):
            await state.stop_erg()
            await mock_release_control(bus, "requested")
        elif isinstance(command, StartCommand):
            await mock_acquire_control(bus)

    handler = on_command if allow_trainer_control else None

    async with (
        run_ws_server(bus, port=ws_port, on_command=handler),
        run_web_ui(state, port=ui_port),
    ):
        if allow_trainer_control:
            # Mock auto-acquires (live mode does the same right after connect)
            # so the engine sees control_acquired without having to issue Start.
            await mock_acquire_control(bus)
        await run_mock_loop(bus, state)


async def _disconnect_bailout_watcher(
    bus: EventBus,
    sources: list[BleSource],
    grace_s: float,
) -> None:
    """If every WS client drops while a bike is under our control, wait
    ``grace_s`` and then release control. Cancels its own grace timer if a
    client reconnects before the window expires.

    Polls at 1 Hz — coarse, fine for a safety net. A more precise hook would
    be a bus-level "subscribers_changed" signal, worth adding if we grow more
    things that care about it."""
    if grace_s <= 0:
        return
    armed_at: float | None = None
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(1.0)
        any_controlling = any(s.control is not None and s.control.is_controlling for s in sources)
        if bus.subscriber_count == 0 and any_controlling:
            if armed_at is None:
                armed_at = loop.time()
                log.warning(
                    "all WS clients disconnected while trainer is under control; "
                    "issuing Stop in %.0fs unless a client reconnects",
                    grace_s,
                )
            elif loop.time() - armed_at >= grace_s:
                log.warning("disconnect bailout: releasing trainer control")
                for s in sources:
                    if s.control is not None and s.control.is_controlling:
                        with contextlib.suppress(Exception):
                            await s.control.release("ws_disconnect_bailout")
                armed_at = None  # don't re-fire until conditions change
        else:
            if armed_at is not None:
                log.info("client reconnected; bailout disarmed")
            armed_at = None


async def _run_live(  # noqa: PLR0913 — CLI fan-in
    ws_port: int,
    device_bike: str | None,
    device_hr: str | None,
    prefer_bike_hr: bool,
    *,
    allow_trainer_control: bool,
    max_target_power: int,
    min_target_power: int,
    disconnect_bailout_s: float,
) -> None:
    bus = EventBus()
    bike_profile = FtmsBikeProfile()
    hr_profile = HrsProfile()

    requested: list[BleProfile] = []
    if device_bike is not None:
        requested.append(bike_profile)
    if device_hr is not None:
        requested.append(hr_profile)

    log.info(
        "scanning for %s ...",
        " + ".join(p.name for p in requested),
    )
    discovered = await scan_for_profiles(requested)

    bike_match: DiscoveredDevice | None = None
    hr_match: DiscoveredDevice | None = None

    if device_bike is not None:
        bike_match = _match_device(discovered[bike_profile.name], device_bike)
        if bike_match is None:
            click.echo(
                f"no FTMS bike matched {device_bike!r}. Found: "
                f"{[d.name for d in discovered[bike_profile.name]] or '(none)'}",
                err=True,
            )
            sys.exit(1)

    if device_hr is not None:
        hr_match = _match_device(discovered[hr_profile.name], device_hr)
        if hr_match is None:
            click.echo(
                f"no HR sensor matched {device_hr!r}. Found: "
                f"{[d.name for d in discovered[hr_profile.name]] or '(none)'}",
                err=True,
            )
            sys.exit(1)

    bike_drops, hr_drops = _resolve_drop_types(
        has_bike=bike_match is not None,
        has_hr=hr_match is not None,
        prefer_bike_hr=prefer_bike_hr,
    )

    bike_source: BleSource | None = None
    sources: list[BleSource] = []
    if bike_match is not None:
        log.info("connecting to %s (%s)", bike_match.name, bike_match.address)
        # The control factory is closed over with the rider's safety bounds;
        # the bike source invokes it post-connect once it has a live client.
        bike_name = bike_match.name

        async def _factory(client: BleakClient) -> FtmsControl | None:
            if not allow_trainer_control:
                return None
            return FtmsControl(
                client,
                bus,
                device_kind=bike_profile.device_kind,
                device_name=bike_name,
                max_watts=max_target_power,
                min_watts=min_target_power,
            )

        bike_source = BleSource(
            bike_profile,
            bike_match.address,
            bike_match.name,
            bus,
            drop_event_types=bike_drops,
            control_factory=_factory if allow_trainer_control else None,
        )
        sources.append(bike_source)
    if hr_match is not None:
        log.info("connecting to %s (%s)", hr_match.name, hr_match.address)
        sources.append(
            BleSource(
                hr_profile,
                hr_match.address,
                hr_match.name,
                bus,
                drop_event_types=hr_drops,
            )
        )

    # The command handler dispatches engine→sidecar commands to whichever
    # source actually owns the trainer (currently only the bike).
    async def on_command(command: Command) -> None:
        if isinstance(command, SetTargetPowerCommand):
            if bike_source is None or bike_source.control is None:
                await bus.publish(
                    type_="target_power_set",
                    data=TargetPowerSetData(
                        watts=command.watts,
                        accepted=False,
                        reason="no controllable bike",
                    ),
                    device_kind=bike_profile.device_kind,
                )
                return
            await bike_source.control.set_target_power(command.watts)
        elif isinstance(command, StopCommand):
            if bike_source is not None and bike_source.control is not None:
                await bike_source.control.stop()
        elif isinstance(command, ReleaseControlCommand):
            if bike_source is not None and bike_source.control is not None:
                await bike_source.control.release("requested")
        elif isinstance(command, StartCommand):
            if (
                bike_source is not None
                and bike_source.control is not None
                and not bike_source.control.is_controlling
            ):
                await bike_source.control.request_control_and_start()

    primary_kind = sources[0]._profile.device_kind  # noqa: SLF001 — own-package attr
    handler = on_command if allow_trainer_control else None

    async with run_ws_server(bus, port=ws_port, on_command=handler):
        await announce_session_start(bus, primary_kind)
        tasks = [asyncio.create_task(s.run()) for s in sources]
        if allow_trainer_control:
            tasks.append(
                asyncio.create_task(_disconnect_bailout_watcher(bus, sources, disconnect_bailout_s))
            )
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()


async def _run_scan() -> None:
    discovered = await scan_for_profiles(ALL_PROFILES)
    any_found = any(devices for devices in discovered.values())
    if not any_found:
        click.echo("no devices found for any known profile.")
        return
    first = True
    for profile_name, devices in discovered.items():
        if not first:
            click.echo("")
        first = False
        click.echo(f"{profile_name}:")
        if not devices:
            click.echo("  (none)")
            continue
        click.echo(f"  {'address':<20}  {'rssi':>5}  name")
        for d in devices:
            rssi = f"{d.rssi}" if d.rssi is not None else "?"
            click.echo(f"  {d.address:<20}  {rssi:>5}  {d.name}")


@click.command()
@click.option(
    "--mode",
    type=click.Choice(["mock", "live", "replay"], case_sensitive=False),
    default="mock",
    show_default=True,
    help="Event source. 'replay' is not yet implemented.",
)
@click.option(
    "--scan",
    "scan_only",
    is_flag=True,
    help="Scan for known BLE profiles (FTMS bike + HR sensor) and exit.",
)
@click.option(
    "--device-bike",
    "device_bike",
    type=str,
    default=None,
    help="Name (full or substring) or BLE address of the FTMS bike trainer.",
)
@click.option(
    "--device-hr",
    "device_hr",
    type=str,
    default=None,
    help="Name (full or substring) or BLE address of the standalone HR sensor.",
)
@click.option(
    "--prefer-bike-hr",
    is_flag=True,
    help=(
        "When both --device-bike and --device-hr are set, publish the bike's "
        "embedded HR rather than the standalone sensor's. Default is to prefer "
        "the standalone sensor."
    ),
)
@click.option(
    "--allow-trainer-control",
    is_flag=True,
    help=(
        "Opt in to trainer control (FTMS Control Point writes). When set, the "
        "sidecar will Request Control + Start at connect, accept "
        "set_target_power / stop / release_control commands over the WS, and "
        "release control on shutdown. Default off."
    ),
)
@click.option(
    "--max-target-power",
    type=int,
    default=DEFAULT_MAX_TARGET_POWER,
    show_default=True,
    help="Maximum target power (watts) the sidecar will write to the trainer.",
)
@click.option(
    "--min-target-power",
    type=int,
    default=DEFAULT_MIN_TARGET_POWER,
    show_default=True,
    help="Minimum target power (watts). 0 allows ERG free spin.",
)
@click.option(
    "--disconnect-bailout-s",
    type=float,
    default=DEFAULT_DISCONNECT_BAILOUT_S,
    show_default=True,
    help=(
        "If all WS clients disconnect while the trainer is under control, "
        "wait this many seconds before issuing Stop. 0 disables the bailout."
    ),
)
@click.option("--ws-port", type=int, default=WS_PORT, show_default=True)
@click.option("--ui-port", type=int, default=UI_PORT, show_default=True)
def main(  # noqa: PLR0913 — CLI fan-in
    mode: str,
    scan_only: bool,
    device_bike: str | None,
    device_hr: str | None,
    prefer_bike_hr: bool,
    allow_trainer_control: bool,
    max_target_power: int,
    min_target_power: int,
    disconnect_bailout_s: float,
    ws_port: int,
    ui_port: int,
) -> None:
    """Run the roguERGlike sidecar."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if scan_only:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_run_scan())
        return

    if mode == "mock":
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(
                _run_mock(
                    ws_port,
                    ui_port,
                    allow_trainer_control=allow_trainer_control,
                )
            )
        return

    if mode == "live":
        if device_bike is None and device_hr is None:
            click.echo(
                "--mode live requires at least one of --device-bike / --device-hr",
                err=True,
            )
            sys.exit(2)
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(
                _run_live(
                    ws_port,
                    device_bike,
                    device_hr,
                    prefer_bike_hr,
                    allow_trainer_control=allow_trainer_control,
                    max_target_power=max_target_power,
                    min_target_power=min_target_power,
                    disconnect_bailout_s=disconnect_bailout_s,
                )
            )
        return

    click.echo(f"mode={mode!r} not yet implemented", err=True)
    sys.exit(2)


if __name__ == "__main__":
    main()
