"""Tiny helpers for session-lifecycle events shared by mock + live modes."""

from __future__ import annotations

from .events import DeviceKind, SessionStartData
from .ws_server import EventBus


async def announce_session_start(bus: EventBus, device_kind: DeviceKind) -> None:
    """Publish ``session_start`` once per sidecar process.

    The bus already owns the session id; we just surface it on the wire so
    clients (engine, future replay tooling) know when a new session began.
    """
    await bus.publish(
        type_="session_start",
        data=SessionStartData(session_id=bus.session_id),
        device_kind=device_kind,
    )
