"""WebSocket transport.

Two flows on one connection:

- **Outbound (sidecar → client):** events. A producer (mock loop, BLE source,
  control client) calls ``EventBus.publish``; the bus wraps payloads in an
  ``Envelope`` (monotonic seq + ts + session_id) and fans out to every
  connected client.
- **Inbound (client → sidecar):** commands. Each connection runs a receive
  task that parses messages as :data:`events.Command` and hands them to an
  optional ``on_command`` callback. Used by the engine to send Set Target
  Power / Stop while the trainer is under sidecar control.

If no ``on_command`` is configured, inbound messages are logged and dropped.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import websockets
from pydantic import ValidationError
from websockets.asyncio.server import ServerConnection, serve

from .events import (
    AnnotateCommand,
    Command,
    DeviceKind,
    Envelope,
    EventData,
    EventType,
    RiderAnnotationData,
    SetTargetPowerCommand,
    TargetPowerSetData,
    now_ts,
    parse_command_json,
)

CommandHandler = Callable[[Command], Awaitable[None]]

log = logging.getLogger(__name__)

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8421

# Events that describe session-level state a client needs to interpret the
# live stream. The bus remembers the most recent envelope of each type and
# replays them to every new subscriber so a client that connects mid-session
# still learns: the session id, what device is attached, what the device can
# do, and whether the sidecar currently holds control. Per-tick telemetry
# (power, cadence, heart_rate, ...) is deliberately NOT replayed.
#
# ``target_power_set`` is deliberately NOT replayed either — it's an
# acknowledgement of a specific command rather than ambient state. An engine
# reconnecting can issue a fresh ``set_target_power`` if it wants to assert
# the current ERG target.
SESSION_STATE_TYPES: frozenset[EventType] = frozenset(
    {
        "hello",
        "session_start",
        "device_connected",
        "device_disconnected",
        "device_capabilities",
        "control_acquired",
        "control_released",
    }
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

    @property
    def subscriber_count(self) -> int:
        """How many WS clients are currently subscribed. Used by the live
        runner's disconnect-bailout watcher — when all clients drop while a
        trainer is under our control, the runner waits a grace window then
        issues Stop so the trainer doesn't keep holding the last target
        indefinitely."""
        return len(self._subscribers)

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


async def _publish_no_handler_rejection(bus: EventBus, command: Command) -> None:
    """Surface a typed rejection event when a command arrives with no handler.

    For commands that have a paired ack event (currently only
    ``set_target_power`` → ``target_power_set``), emit the ack with
    ``accepted=false`` and an explicit reason. Other commands fall through
    to a warning log — they have no client-observable ack envelope to fill,
    and the dispatcher being unconfigured is the same condition for all of
    them, so we don't need duplicate rejections.
    """
    if isinstance(command, SetTargetPowerCommand):
        await bus.publish(
            type_="target_power_set",
            data=TargetPowerSetData(
                watts=command.watts,
                accepted=False,
                reason="trainer-control disabled (no --allow-trainer-control flag)",
            ),
            device_kind="bike_trainer",
        )
        return
    log.warning(
        "received command %s but no handler is configured (sidecar not "
        "running with --allow-trainer-control?)",
        command.type,
    )


async def _send_loop(
    connection: ServerConnection,
    queue: asyncio.Queue[Envelope],
) -> None:
    while True:
        env = await queue.get()
        await connection.send(env.to_wire())


async def _publish_annotation(bus: EventBus, command: AnnotateCommand) -> None:
    """Republish an inbound annotate command as a ``rider_annotation`` envelope.

    Always-on: annotations are inert with respect to the trainer (no FTMS
    writes, no state change) so they don't need ``--allow-trainer-control``.
    Handled at the WS layer rather than through ``on_command`` because the
    transformation is purely schema-level and identical across mock/live."""
    await bus.publish(
        type_="rider_annotation",
        data=RiderAnnotationData(
            tag=command.tag,
            note=command.note,
            client_id=command.client_id,
            client_time_s=command.client_time_s,
            context=command.context,
        ),
        device_kind="client",
    )


async def _recv_loop(
    connection: ServerConnection,
    bus: EventBus,
    on_command: CommandHandler | None,
) -> None:
    """Read inbound messages, validate as :data:`Command`, dispatch.

    Annotations bypass ``on_command`` and are published directly. For other
    commands, when ``on_command`` is None (the sidecar wasn't started with
    ``--allow-trainer-control``), commands that have a paired ack event get
    an explicit rejection envelope so clients can distinguish "flag missing"
    from "device not present" from "trainer rejected". The previous
    log-and-drop behaviour was a silent footgun on the engine side — see
    project memory ``opt-in-flags-need-feedback``.
    """
    async for message in connection:
        if not isinstance(message, str):
            log.warning("ignoring non-text WS frame")
            continue
        try:
            command = parse_command_json(message)
        except ValidationError as e:
            log.warning("dropping malformed command: %s", e.errors()[0]["msg"] if e.errors() else e)
            continue
        if isinstance(command, AnnotateCommand):
            await _publish_annotation(bus, command)
            continue
        if on_command is None:
            await _publish_no_handler_rejection(bus, command)
            continue
        try:
            await on_command(command)
        except Exception:  # noqa: BLE001 — one bad command must not kill the WS
            log.exception("error handling command %s", command.type)


async def _handle_client(
    connection: ServerConnection,
    bus: EventBus,
    on_command: CommandHandler | None,
) -> None:
    peer = connection.remote_address
    log.info("client connected: %s", peer)
    async with bus.subscribe() as queue:
        send = asyncio.create_task(_send_loop(connection, queue))
        recv = asyncio.create_task(_recv_loop(connection, bus, on_command))
        try:
            # Either side ending (clean disconnect on recv, error on either)
            # closes the whole client connection.
            done, pending = await asyncio.wait({send, recv}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, websockets.ConnectionClosed):
                    log.exception(
                        "client task ended with error", exc_info=(type(exc), exc, exc.__traceback__)
                    )
        finally:
            log.info("client disconnected: %s", peer)


@asynccontextmanager
async def run_ws_server(
    bus: EventBus,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    on_command: CommandHandler | None = None,
) -> AsyncIterator[Any]:
    """Start the WS server bound to ``bus``; yields the server until cancellation.

    ``on_command``, if provided, is awaited once per validated inbound command
    message. The handler is shared across all connected clients; if you need
    per-client state, close over it before passing in."""

    async def handler(conn: ServerConnection) -> None:
        await _handle_client(conn, bus, on_command)

    async with serve(handler, host, port) as server:
        log.info("sidecar WS listening on ws://%s:%d", host, port)
        yield server
