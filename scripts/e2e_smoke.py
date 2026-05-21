"""End-to-end smoke test: boot mock mode in-process, connect a WS client,
read a handful of envelopes, set a slider value via the UI control endpoint,
verify the next power event reflects it."""

from __future__ import annotations

import asyncio
import json

import aiohttp
import websockets
from roguerglike_sidecar.mock import MockState, run_mock_loop
from roguerglike_sidecar.web_ui import run_web_ui
from roguerglike_sidecar.ws_server import EventBus, run_ws_server

WS_PORT = 18421
UI_PORT = 18422


async def main() -> None:
    bus = EventBus()
    state = MockState()
    async with run_ws_server(bus, port=WS_PORT), run_web_ui(state, port=UI_PORT):
        producer = asyncio.create_task(run_mock_loop(bus, state, hz=10.0))
        try:
            # Give the server a tick to bind, then connect.
            await asyncio.sleep(0.1)
            async with websockets.connect(f"ws://localhost:{WS_PORT}") as ws:
                # Drain a few startup events to confirm the wire works.
                for _ in range(4):
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    print("got:", msg)

                # Drive the UI control endpoint and confirm the next power
                # event reflects the new slider value.
                async with aiohttp.ClientSession() as http:
                    await http.post(
                        f"http://localhost:{UI_PORT}/control",
                        json={"watts": 271},
                    )
                deadline = asyncio.get_event_loop().time() + 2.0
                while asyncio.get_event_loop().time() < deadline:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    env = json.loads(msg)
                    if env["type"] == "power" and env["data"]["watts"] == 271:
                        print("control round-trip OK")
                        return
                raise SystemExit("did not observe updated power event in time")
        finally:
            producer.cancel()


if __name__ == "__main__":
    asyncio.run(main())
