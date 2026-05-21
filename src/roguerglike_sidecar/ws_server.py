"""WebSocket broadcaster.

A single producer (the mock loop, or later a BLE deriver) calls ``EventBus.publish``
with an event ``data`` model and a type/device_kind; the bus wraps it in an
``Envelope`` (assigning monotonic seq + ts + session_id) and fans it out to every
connected client. The engine's ``effort_bridge.gd`` is one such client.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import websockets
from websockets.asyncio.server import ServerConnection, serve

from .events import DeviceKind, Envelope, EventData, EventType, now_ts

log = logging.getLogger(__name__)

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8421


class EventBus:
    """In-process pub/sub used by the mock producer and the WS server.

    Holds session_id + seq state. ``publish`` is the only writer; ``subscribe``
    yields envelopes for each connected client.
    """

    def __init__(self) -> None:
        self.session_id: UUID = uuid4()
        self._seq = 0
        self._subscribers: set[asyncio.Queue[Envelope]] = set()
        self._lock = asyncio.Lock()

    async def publish(
        self,
        *,
        type_: EventType,
        data: EventData,
        device_kind: DeviceKind,
    ) -> Envelope:
        async with self._lock:
            seq = self._seq
            self._seq += 1
        env = Envelope(
            type=type_,
            ts=now_ts(),
            session_id=self.session_id,
            seq=seq,
            device_kind=device_kind,
            data=data,
        )
        for q in list(self._subscribers):
            try:
                q.put_nowait(env)
            except asyncio.QueueFull:
                log.warning("dropping event for slow subscriber")
        return env

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Envelope]]:
        q: asyncio.Queue[Envelope] = asyncio.Queue(maxsize=256)
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)


async def _handle_client(connection: ServerConnection, bus: EventBus) -> None:
    peer = connection.remote_address
    log.info("client connected: %s", peer)
    async with bus.subscribe() as queue:
        try:
            while True:
                env = await queue.get()
                await connection.send(env.to_wire())
        except websockets.ConnectionClosed:
            log.info("client disconnected: %s", peer)


@asynccontextmanager
async def run_ws_server(
    bus: EventBus,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> AsyncIterator[Any]:
    """Start the WS server bound to ``bus``; yields the server until cancellation."""

    async def handler(conn: ServerConnection) -> None:
        await _handle_client(conn, bus)

    async with serve(handler, host, port) as server:
        log.info("sidecar WS listening on ws://%s:%d", host, port)
        yield server
