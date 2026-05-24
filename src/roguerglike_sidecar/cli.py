"""Entry point: ``roguerglike-sidecar --mode {mock|live}`` plus ``--scan``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import click

from .ble.cadence_bailout import CadenceBailout
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
from .recording import run_recorder
from .session import announce_hello, announce_session_start, build_features
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
# Static fallbacks when --rider-ftp is not provided. With FTP, both these
# values are replaced by an intensity-aware curve (see CadenceBailout).
DEFAULT_CADENCE_BAILOUT_S = 60.0
DEFAULT_TARGET_POWER_RAMP_S = 3.0


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
    record_path: Path | None,
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
        recorder_task: asyncio.Task[None] | None = None
        if record_path is not None:
            recorder_task = asyncio.create_task(run_recorder(bus, record_path))
        try:
            # hello goes first so it occupies seq=0 in every recording and
            # is the first envelope replayed to every subscriber.
            await announce_hello(
                bus,
                mode="mock",
                features=build_features(allow_trainer_control=allow_trainer_control),
            )
            if allow_trainer_control:
                # Mock auto-acquires (live mode does the same right after connect)
                # so the engine sees control_acquired without having to issue Start.
                await mock_acquire_control(bus)
            await run_mock_loop(bus, state)
        finally:
            if recorder_task is not None:
                recorder_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await recorder_task


DEFAULT_SCAN_TIMEOUT_S = 30.0


def _resolve_matches(
    discovered: dict[str, list[DiscoveredDevice]],
    queries: dict[str, str],
    *,
    already: dict[str, DiscoveredDevice] | None = None,
) -> tuple[dict[str, DiscoveredDevice], list[str]]:
    """Match each user-supplied query against a scan result. Returns the
    updated ``(matched, still_missing)`` pair where ``matched`` maps
    profile_name → DiscoveredDevice and ``still_missing`` is the list of
    profile names whose queries didn't match yet.

    Queries already satisfied (in ``already``) are passed through unchanged
    so re-scans don't re-match what's already been found."""
    matched: dict[str, DiscoveredDevice] = dict(already or {})
    missing: list[str] = []
    for profile_name, query in queries.items():
        if profile_name in matched:
            continue
        hit = _match_device(discovered.get(profile_name, []), query)
        if hit is not None:
            matched[profile_name] = hit
        else:
            missing.append(profile_name)
    return matched, missing


async def _scan_with_retry(
    profiles: list[BleProfile],
    queries: dict[str, str],
    *,
    total_timeout_s: float,
    per_scan_timeout_s: float = 8.0,
) -> dict[str, DiscoveredDevice] | None:
    """Scan repeatedly until every query in ``queries`` matches a device, or
    ``total_timeout_s`` elapses. Logs what's still missing between cycles so
    the operator knows whether to wake a sleeping trainer or re-enable Whoop
    broadcast mid-test. Returns ``None`` on timeout."""
    deadline = time.monotonic() + max(total_timeout_s, per_scan_timeout_s)
    matched: dict[str, DiscoveredDevice] = {}
    attempt = 0
    while True:
        attempt += 1
        log.info(
            "scan attempt %d for %s ...",
            attempt,
            " + ".join(p.name for p in profiles),
        )
        discovered = await scan_for_profiles(profiles, timeout_s=per_scan_timeout_s)
        matched, missing = _resolve_matches(discovered, queries, already=matched)
        if not missing:
            for name, device in matched.items():
                log.info("matched %s → %s (%s)", name, device.name, device.address)
            return matched
        if time.monotonic() >= deadline:
            click.echo(
                f"scan timed out after {total_timeout_s:.0f}s; still waiting for: "
                f"{', '.join(missing)}",
                err=True,
            )
            return None
        log.warning(
            "still waiting for %s — will keep scanning (%.0fs left in window)",
            ", ".join(missing),
            deadline - time.monotonic(),
        )


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
    cadence_bailout_s: float,
    target_power_ramp_s: float,
    rider_ftp: int | None,
    scan_timeout_s: float,
    record_path: Path | None,
) -> None:
    bus = EventBus()
    bike_profile = FtmsBikeProfile()
    hr_profile = HrsProfile()

    requested: list[BleProfile] = []
    queries: dict[str, str] = {}  # profile_name -> user's --device-* query
    if device_bike is not None:
        requested.append(bike_profile)
        queries[bike_profile.name] = device_bike
    if device_hr is not None:
        requested.append(hr_profile)
        queries[hr_profile.name] = device_hr

    matches = await _scan_with_retry(requested, queries, total_timeout_s=scan_timeout_s)
    if matches is None:
        sys.exit(1)

    bike_match: DiscoveredDevice | None = matches.get(bike_profile.name)
    hr_match: DiscoveredDevice | None = matches.get(hr_profile.name)

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

    # Set up the cadence bailout if trainer control is enabled. We construct
    # it lazily-bound to the bike source's control client (which doesn't
    # exist until post-connect). The handler captures `bike_source` and
    # reads `.control` and `._bailout` each call, so they get the live
    # instances when the command actually fires.
    bailout_for_bike: CadenceBailout | None = None

    # The command handler dispatches engine→sidecar commands to whichever
    # source actually owns the trainer (currently only the bike). When the
    # cadence bailout is active, set_target_power calls are routed through
    # it so paused-state queueing works correctly.
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
            if bailout_for_bike is not None:
                await bailout_for_bike.handle_set_target_power(command.watts)
            else:
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
        # hello goes first so it occupies seq=0 in every recording and
        # is the first envelope replayed to every subscriber.
        await announce_hello(
            bus,
            mode="live",
            features=build_features(allow_trainer_control=allow_trainer_control),
        )
        await announce_session_start(bus, primary_kind)
        tasks = [asyncio.create_task(s.run()) for s in sources]
        if record_path is not None:
            tasks.append(asyncio.create_task(run_recorder(bus, record_path)))
        if allow_trainer_control:
            tasks.append(
                asyncio.create_task(_disconnect_bailout_watcher(bus, sources, disconnect_bailout_s))
            )
            if bike_source is not None:
                # Spin up the bailout immediately; it'll only do anything
                # once the bike source has connected and gained a control
                # client. The factory closes over the bike source so it can
                # rebind to the live control as connects come and go.
                bailout_for_bike = await _spawn_cadence_bailout(
                    bike_source,
                    bus,
                    bike_profile.device_kind,
                    bike_match.name if bike_match else "bike",
                    rider_ftp=rider_ftp,
                    static_bailout_s=cadence_bailout_s,
                    static_ramp_s=target_power_ramp_s,
                    tasks=tasks,
                )
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()


async def _spawn_cadence_bailout(  # noqa: PLR0913 — runner internal
    bike_source: BleSource,
    bus: EventBus,
    device_kind: str,
    device_name: str,
    *,
    rider_ftp: int | None,
    static_bailout_s: float,
    static_ramp_s: float,
    tasks: list[asyncio.Task[None]],
) -> CadenceBailout:
    """Wait for the bike's FtmsControl to come online, then construct +
    start the CadenceBailout. The bailout subscribes to bus cadence events,
    so we don't need a tight coupling between source attach and bailout
    spin-up — the bailout simply does nothing until ``control`` exists and
    ``is_controlling`` is true. We do however need a *non-None* FtmsControl
    reference to construct it. Hence the short wait."""
    while bike_source.control is None:
        await asyncio.sleep(0.5)
    bailout = CadenceBailout(
        bike_source.control,
        bus,
        device_kind=device_kind,  # type: ignore[arg-type]
        device_name=device_name,
        rider_ftp=rider_ftp,
        static_bailout_s=static_bailout_s,
        static_ramp_s=static_ramp_s,
    )
    tasks.append(asyncio.create_task(bailout.run()))
    return bailout


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
@click.option(
    "--cadence-bailout-s",
    type=float,
    default=DEFAULT_CADENCE_BAILOUT_S,
    show_default=True,
    help=(
        "If cadence stays below ~30 rpm for this many seconds while ERG is "
        "active, drop the target to --min-target-power so the cranks free up. "
        "Used only as a static fallback; with --rider-ftp set, the value is "
        "computed dynamically per current intensity. Resume is automatic on "
        "cadence ≥ ~30 rpm."
    ),
)
@click.option(
    "--target-power-ramp-s",
    type=float,
    default=DEFAULT_TARGET_POWER_RAMP_S,
    show_default=True,
    help=(
        "Duration of the resume ramp (seconds) when cadence comes back after "
        "a bailout. Used only as a static fallback; with --rider-ftp set, the "
        "value is computed dynamically per current intensity."
    ),
)
@click.option(
    "--rider-ftp",
    "rider_ftp",
    type=int,
    default=None,
    help=(
        "Rider's FTP in watts. When set, the cadence-bailout wait time and "
        "resume-ramp duration both scale with the current intensity "
        "(target / FTP). At low intensity: patient bailout + fast ramp. At "
        "high intensity: fast bailout + slow ramp. Without this flag, the "
        "static --cadence-bailout-s and --target-power-ramp-s values are "
        "used at all intensities."
    ),
)
@click.option(
    "--scan-timeout-s",
    type=float,
    default=DEFAULT_SCAN_TIMEOUT_S,
    show_default=True,
    help=(
        "Total seconds to keep retrying BLE scans before giving up. Each "
        "scan attempt is ~8s; the sidecar prints what's still missing "
        "between attempts so you can wake a sleeping trainer or re-enable "
        "Whoop broadcast without having to re-launch the command."
    ),
)
@click.option(
    "--record",
    "record_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Write every published envelope to this JSONL file (one JSON object "
        "per line, flushed per write). Includes the session-state replay so "
        "the recording is self-contained. The file WILL contain personal "
        "telemetry (HR, power, cadence) — keep it out of git."
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
    cadence_bailout_s: float,
    target_power_ramp_s: float,
    rider_ftp: int | None,
    scan_timeout_s: float,
    record_path: Path | None,
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
                    record_path=record_path,
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
                    cadence_bailout_s=cadence_bailout_s,
                    target_power_ramp_s=target_power_ramp_s,
                    rider_ftp=rider_ftp,
                    scan_timeout_s=scan_timeout_s,
                    record_path=record_path,
                )
            )
        return

    click.echo(f"mode={mode!r} not yet implemented", err=True)
    sys.exit(2)


if __name__ == "__main__":
    main()
