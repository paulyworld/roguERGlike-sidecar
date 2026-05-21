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

# Events that describe session-level state a client needs to interpret the live
# stream. The bus remembers the most recent envelope of each type and replays
# them to every new subscriber so a client that connects mid-session still
# learns the session id and which device is attached. Per-tick telemetry
# (power, cadence, heart_rate, ...) is deliberately NOT replayed.
SESSION_STATE_TYPES: frozenset[EventType] = frozenset(
    {"session_start", "device_connected", "device_disconnected"}
)


class EventBus:
    """In-process pub/sub used by the mock producer and the WS server.

    Holds session_id + seq state and the latest envelope of each session-state
    event type. ``publish`` is the only writer; ``subscribe`` yields a queue
    that is prefilled with current session-state events in seq order before any
    live events arrive.
    """

    def __init__(self) -> None:
        self.session_id: UUID = uuid4()
        self._seq = 0
        self._subscribers: set[asyncio.Queue[Envelope]] = set()
        self._session_state: dict[EventType, Envelope] = {}
        self._lock = asyncio.Lock()

    async def publish(
        self,
        *,
        type_: EventType,
        data: EventData,
        device_kind: DeviceKind,
    ) -> Envelope:
        # Take the lock for the full critical section: seq assignment, envelope
        # construction, session-state update, AND the subscriber snapshot. This
        # closes the race where a new subscriber could attach between state
        # update and fan-out and receive both a replay AND the live event.
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
            if type_ in SESSION_STATE_TYPES:
                self._session_state[type_] = env
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait(env)
            except asyncio.QueueFull:
                log.warning("dropping event for slow subscriber")
        return env

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Envelope]]:
        q: asyncio.Queue[Envelope] = asyncio.Queue(maxsize=256)
        async with self._lock:
            for env in sorted(self._session_state.values(), key=lambda e: e.seq):
                q.put_nowait(env)
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
