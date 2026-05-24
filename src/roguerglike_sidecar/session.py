"""Tiny helpers for session-lifecycle events shared by mock + live modes."""

from __future__ import annotations

from typing import Literal

from . import __version__
from .events import DeviceKind, HelloData, SessionStartData
from .ws_server import EventBus

# Bumped on protocol changes:
#   - minor: added a feature / event type backward-compatibly
#   - major: removed/renamed an event or command shape
# Clients seeing an unknown major should surface a visible warning.
PROTOCOL_VERSION = "1.0.0"

# Protocol features the sidecar currently supports. Each string is a stable
# identifier clients gate UI on (distinct from device capabilities, which
# describe trainer hardware). Add a string here when a new feature ships;
# never rename or remove without bumping the protocol major version.
_BASE_FEATURES: tuple[str, ...] = (
    "set_target_power",
    "recording",
    "annotations",
    "structured_pause",
    "distance",
    "activity_export",
    "indoor_bike_simulation",
)


def build_features(*, allow_trainer_control: bool) -> list[str]:
    """Compose the feature list advertised in the ``hello`` envelope.

    ``set_target_power`` stays on regardless of ``--allow-trainer-control``:
    the *command* is part of the protocol whether or not the sidecar will
    execute it. Runtime gating surfaces via the typed ``target_power_set``
    rejection envelope (per memory ``opt-in-flags-need-feedback``), not via
    feature absence — otherwise a client that connects before the operator
    flips the flag would gate the UI off and never re-enable it."""
    # Currently no flag changes the feature set, but keep the signature
    # flexible so future capabilities (e.g. structured_pause, distance,
    # activity_export, indoor_bike_simulation) can be conditionally added
    # once their write paths land.
    del allow_trainer_control  # explicitly unused for now
    return list(_BASE_FEATURES)


async def announce_hello(
    bus: EventBus,
    *,
    mode: Literal["mock", "live", "replay"],
    features: list[str],
) -> None:
    """Publish the sidecar's self-describing ``hello`` envelope.

    Called once at startup before any other publish, so it ends up first in
    the session-state replay queue every late subscriber receives. The bus
    treats ``hello`` as session-state (see ``SESSION_STATE_TYPES``), so a
    client connecting any time during the session learns the protocol
    version + feature list + mode immediately on attach."""
    await bus.publish(
        type_="hello",
        data=HelloData(
            protocol_version=PROTOCOL_VERSION,
            sidecar_version=__version__,
            features=features,
            mode=mode,
        ),
        device_kind="sidecar",
    )


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
