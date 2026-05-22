"""Pydantic v2 models for the sidecar event schema.

The wire format is newline-delimited JSON objects matching ``docs/event-schema.md``.
Every event shares the ``Envelope`` shape; ``data`` is one of the per-type models
below. The schema is a stable contract — breaking changes require a major bump.
"""

from __future__ import annotations

import time
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

DeviceKind = Literal[
    "bike_trainer",
    "rower",
    "treadmill",
    "cross_trainer",
    "power_meter",
    "hr_sensor",
    "mock",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- per-event data payloads ----------------------------------------------


class PowerData(_StrictModel):
    watts: int = Field(ge=0, le=3000)


class CadenceData(_StrictModel):
    rpm: int = Field(ge=0, le=250)


class HeartRateData(_StrictModel):
    bpm: int = Field(ge=20, le=240)


class SpeedData(_StrictModel):
    kph: float = Field(ge=0.0, le=120.0)


class DistanceData(_StrictModel):
    meters_total: float = Field(ge=0.0)
    meters_delta: float = Field(ge=0.0)


class DeviceConnectedData(_StrictModel):
    kind: DeviceKind
    name: str


class DeviceDisconnectedData(_StrictModel):
    kind: DeviceKind
    name: str


class DeviceCapabilitiesData(_StrictModel):
    """What a connected device can be asked to do.

    Emitted once per device shortly after ``device_connected``. The booleans
    are pure capability flags — they say nothing about whether the host has
    asked to use them or whether the connection currently has control. The
    eventual trainer-control work (FTMS Control Point writes) will be gated
    on ``target_power`` here.
    """

    kind: DeviceKind
    name: str
    # FTMS Target Setting Features (Bluetooth SIG, characteristic 0x2ACC upper
    # 4 bytes). Names track the spec's bit-name fields.
    target_power: bool = False
    target_resistance: bool = False
    target_inclination: bool = False
    target_heart_rate: bool = False
    indoor_bike_simulation: bool = False


class SessionStartData(_StrictModel):
    session_id: UUID


class SessionEndData(_StrictModel):
    session_id: UUID
    duration_s: float = Field(ge=0.0)


EventType = Literal[
    "power",
    "cadence",
    "heart_rate",
    "speed",
    "distance",
    "device_connected",
    "device_disconnected",
    "device_capabilities",
    "session_start",
    "session_end",
]

EventData = (
    PowerData
    | CadenceData
    | HeartRateData
    | SpeedData
    | DistanceData
    | DeviceConnectedData
    | DeviceDisconnectedData
    | DeviceCapabilitiesData
    | SessionStartData
    | SessionEndData
)


class Envelope(BaseModel):
    """Outer wrapper emitted on the wire for every event."""

    model_config = ConfigDict(extra="forbid")

    type: EventType
    ts: float
    session_id: UUID
    seq: Annotated[int, Field(ge=0)]
    device_kind: DeviceKind
    data: EventData

    def to_wire(self) -> str:
        return self.model_dump_json()


def now_ts() -> float:
    """Wall-clock seconds; chosen over monotonic so timestamps survive restarts."""
    return time.time()
