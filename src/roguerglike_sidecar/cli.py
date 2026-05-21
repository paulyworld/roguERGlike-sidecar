"""Entry point: ``roguerglike-sidecar --mode mock`` runs WS + UI + mock producer."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

import click

from .mock import MockState, run_mock_loop
from .web_ui import DEFAULT_PORT as UI_PORT
from .web_ui import run_web_ui
from .ws_server import DEFAULT_PORT as WS_PORT
from .ws_server import EventBus, run_ws_server


async def _run_mock(ws_port: int, ui_port: int) -> None:
    bus = EventBus()
    state = MockState()
    async with run_ws_server(bus, port=ws_port), run_web_ui(state, port=ui_port):
        await run_mock_loop(bus, state)


@click.command()
@click.option(
    "--mode",
    type=click.Choice(["mock", "live", "replay"], case_sensitive=False),
    default="mock",
    show_default=True,
    help="Event source. Only 'mock' is implemented in Phase 1.",
)
@click.option("--ws-port", type=int, default=WS_PORT, show_default=True)
@click.option("--ui-port", type=int, default=UI_PORT, show_default=True)
def main(mode: str, ws_port: int, ui_port: int) -> None:
    """Run the roguERGlike sidecar."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if mode != "mock":
        click.echo(f"mode={mode!r} not yet implemented (Phase 1 ships mock only)", err=True)
        sys.exit(2)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run_mock(ws_port, ui_port))


if __name__ == "__main__":
    main()
