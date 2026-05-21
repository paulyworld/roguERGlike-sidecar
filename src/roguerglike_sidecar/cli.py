"""Entry point: ``roguerglike-sidecar --mode {mock|live}`` plus ``--scan``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

import click

from .ble.ftms_bike import FtmsBikeProfile
from .ble.hrs import HrsProfile
from .ble.profile import BleProfile
from .ble.scan import DiscoveredDevice, scan_for_profiles
from .ble.source import BleSource
from .events import EventType
from .mock import MockState, run_mock_loop
from .session import announce_session_start
from .web_ui import DEFAULT_PORT as UI_PORT
from .web_ui import run_web_ui
from .ws_server import DEFAULT_PORT as WS_PORT
from .ws_server import EventBus, run_ws_server

log = logging.getLogger(__name__)

# Order matters for output and for the order ``--scan`` lists profiles.
ALL_PROFILES: list[BleProfile] = [FtmsBikeProfile(), HrsProfile()]


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


async def _run_mock(ws_port: int, ui_port: int) -> None:
    bus = EventBus()
    state = MockState()
    async with run_ws_server(bus, port=ws_port), run_web_ui(state, port=ui_port):
        await run_mock_loop(bus, state)


async def _run_live(
    ws_port: int,
    device_bike: str | None,
    device_hr: str | None,
    prefer_bike_hr: bool,
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

    sources: list[BleSource] = []
    if bike_match is not None:
        log.info("connecting to %s (%s)", bike_match.name, bike_match.address)
        sources.append(
            BleSource(
                bike_profile,
                bike_match.address,
                bike_match.name,
                bus,
                drop_event_types=bike_drops,
            )
        )
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

    # session_start's device_kind reflects the primary source the engine
    # should associate the session with. Bike if present, otherwise HR.
    primary_kind = sources[0]._profile.device_kind  # noqa: SLF001 — own-package attr

    async with run_ws_server(bus, port=ws_port):
        await announce_session_start(bus, primary_kind)
        await asyncio.gather(*(s.run() for s in sources))


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
@click.option("--ws-port", type=int, default=WS_PORT, show_default=True)
@click.option("--ui-port", type=int, default=UI_PORT, show_default=True)
def main(
    mode: str,
    scan_only: bool,
    device_bike: str | None,
    device_hr: str | None,
    prefer_bike_hr: bool,
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
            asyncio.run(_run_mock(ws_port, ui_port))
        return

    if mode == "live":
        if device_bike is None and device_hr is None:
            click.echo(
                "--mode live requires at least one of --device-bike / --device-hr",
                err=True,
            )
            sys.exit(2)
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_run_live(ws_port, device_bike, device_hr, prefer_bike_hr))
        return

    click.echo(f"mode={mode!r} not yet implemented", err=True)
    sys.exit(2)


if __name__ == "__main__":
    main()
