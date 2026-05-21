"""Entry point: ``roguerglike-sidecar --mode {mock|live}`` plus ``--scan``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

import click

from .ble.ftms_bike import FtmsBikeProfile
from .ble.scan import DiscoveredDevice, scan_for_service
from .ble.source import BleSource
from .mock import MockState, run_mock_loop
from .session import announce_session_start
from .web_ui import DEFAULT_PORT as UI_PORT
from .web_ui import run_web_ui
from .ws_server import DEFAULT_PORT as WS_PORT
from .ws_server import EventBus, run_ws_server

log = logging.getLogger(__name__)


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


async def _run_mock(ws_port: int, ui_port: int) -> None:
    bus = EventBus()
    state = MockState()
    async with run_ws_server(bus, port=ws_port), run_web_ui(state, port=ui_port):
        await run_mock_loop(bus, state)


async def _run_live(ws_port: int, device_query: str) -> None:
    bus = EventBus()
    profile = FtmsBikeProfile()

    log.info("scanning for FTMS bike matching %r ...", device_query)
    discovered = await scan_for_service(profile.service_uuid)
    if not discovered:
        click.echo("no FTMS-advertising devices found; is the trainer awake?", err=True)
        sys.exit(1)
    matched = _match_device(discovered, device_query)
    if matched is None:
        click.echo(f"no FTMS device matched {device_query!r}. Found:", err=True)
        for d in discovered:
            click.echo(f"  {d.address}  {d.name}", err=True)
        sys.exit(1)
    log.info("connecting to %s (%s)", matched.name, matched.address)

    source = BleSource(profile, matched.address, matched.name, bus)
    async with run_ws_server(bus, port=ws_port):
        await announce_session_start(bus, profile.device_kind)
        await source.run()


async def _run_scan() -> None:
    profile = FtmsBikeProfile()
    devices = await scan_for_service(profile.service_uuid)
    if not devices:
        click.echo("no FTMS-advertising devices found.")
        return
    click.echo(f"{'address':<20}  {'rssi':>5}  name")
    for d in devices:
        rssi = f"{d.rssi}" if d.rssi is not None else "?"
        click.echo(f"{d.address:<20}  {rssi:>5}  {d.name}")


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
    help="Scan for FTMS devices and exit (ignores --mode).",
)
@click.option(
    "--device-bike",
    "device_bike",
    type=str,
    default=None,
    help="Name (full or substring) or BLE address of the FTMS bike. Required for --mode live.",
)
@click.option("--ws-port", type=int, default=WS_PORT, show_default=True)
@click.option("--ui-port", type=int, default=UI_PORT, show_default=True)
def main(
    mode: str,
    scan_only: bool,
    device_bike: str | None,
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
        if not device_bike:
            click.echo("--mode live requires --device-bike <name|address>", err=True)
            sys.exit(2)
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_run_live(ws_port, device_bike))
        return

    click.echo(f"mode={mode!r} not yet implemented", err=True)
    sys.exit(2)


if __name__ == "__main__":
    main()
