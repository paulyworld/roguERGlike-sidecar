"""Pydantic v2 models for the sidecar event schema.

The wire format is newline-delimited JSON objects matching ``docs/event-schema.md``.
Every event shares the ``Envelope`` shape; ``data`` is one of the per-type models
below. The schema is a stable contract — breaking changes require a major bump.
"""

from __future__ import annotations

import time
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

DeviceKind = Literal[
    "bike_trainer",
    "rower",
    "treadmill",
    "cross_trainer",
    "power_meter",
    "hr_sensor",
    "mock",
    # Non-device source. Used for events that originate from a client
    # (rider annotations, future client-side markers) and don't describe
    # a piece of hardware.
    "client",
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


class ControlAcquiredData(_StrictModel):
    """The sidecar has successfully claimed the device's Fitness Machine
    Control Point (Request Control + Start both acknowledged). From this
    point, ``set_target_power`` commands have effect."""

    kind: DeviceKind
    name: str


class ControlReleasedData(_StrictModel):
    """Control of the device has been released — either gracefully via the
    sidecar issuing Stop, or implicitly via a BLE disconnect, or because the
    engine WS dropped past the safety grace window."""

    kind: DeviceKind
    name: str
    reason: str


class TargetPowerSetData(_StrictModel):
    """Acknowledges (or rejects) a ``set_target_power`` command. ``watts`` is
    the actual value sent to the trainer after sidecar-side clamping; for a
    rejected command it's the unclamped requested value with ``accepted``
    False and ``reason`` populated."""

    watts: int
    accepted: bool
    reason: str = ""


class CadenceBailoutEngagedData(_StrictModel):
    """Cadence has been below the active threshold long enough that the
    sidecar dropped the trainer's target to its configured floor so an
    absent rider isn't left with locked-up cranks. The engine should treat
    this as a soft pause: visually indicate it, optionally pause workout
    timers. The trainer is still under control; resume happens automatically
    on cadence ≥ resume threshold."""

    kind: DeviceKind
    name: str
    pre_pause_target_watts: int
    bailout_after_s: float


class CadenceBailoutDisengagedData(_StrictModel):
    """Cadence resumed; the sidecar has finished ramping the target back to
    the value it was at when the bailout engaged (or whatever the engine has
    asked for since, if it issued ``set_target_power`` during the pause).
    Workout / UI can return to its active state."""

    kind: DeviceKind
    name: str
    restored_to_watts: int
    ramped_over_s: float


class SessionStartData(_StrictModel):
    session_id: UUID


class SessionEndData(_StrictModel):
    session_id: UUID
    duration_s: float = Field(ge=0.0)


class RiderAnnotationData(_StrictModel):
    """Rider-initiated mark on the event stream. Tags are free-form (the
    schema only constrains length) so different clients can converge on a
    shared vocabulary without the sidecar gatekeeping. Recommended tags
    cover three families: tuning feedback (``too-hard``, ``too-easy``,
    ``bad-sync``, ``false-intensity``, ``missed-intensity``,
    ``cadence-mismatch``); ride flow (``ui-pause``, ``walk-away``); and
    catch-alls (``bug``, ``marker``).

    ``client_time_s`` is the rider's video/workout position at the
    keypress, distinct from the sidecar wall-clock ``ts``. Lets analyzers
    position markers on the ride timeline rather than the receipt
    timeline (which drifts on slow WS / buffering).

    ``context`` is a free-form pass-through blob — the sidecar treats it
    as opaque. Recommended fields by convention (clients should converge,
    sidecar doesn't enforce): ``profile_id``, ``profile_version``,
    ``mode`` (e.g. ``terrain_erg`` / ``terrain_sim`` / ``raw_feel``),
    ``video_id``, ``section``, ``target_watts``, ``power``, ``cadence``,
    ``hr``, ``wkg``, ``grade``, ``speed_kph``, ``distance_m``,
    ``elevation_gain_m``, ``hardware_source``."""

    tag: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=280)
    client_id: str | None = Field(default=None, max_length=64)
    client_time_s: float | None = Field(default=None, ge=0.0)
    context: dict[str, object] | None = Field(default=None)


EventType = Literal[
    "power",
    "cadence",
    "heart_rate",
    "speed",
    "distance",
    "device_connected",
    "device_disconnected",
    "device_capabilities",
    "control_acquired",
    "control_released",
    "target_power_set",
    "cadence_bailout_engaged",
    "cadence_bailout_disengaged",
    "session_start",
    "session_end",
    "rider_annotation",
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
    | ControlAcquiredData
    | ControlReleasedData
    | TargetPowerSetData
    | CadenceBailoutEngagedData
    | CadenceBailoutDisengagedData
    | SessionStartData
    | SessionEndData
    | RiderAnnotationData
)


# Map event type literals to the concrete data class that carries their
# payload. Used by Envelope's pre-validator to pick the right class — without
# this, identically-shaped data models (DeviceConnectedData vs
# ControlAcquiredData, both ``{kind, name}``) get round-tripped as the first
# union member, which is correct on the wire but loses type identity in
# Python consumers.
_DATA_BY_TYPE: dict[str, type[BaseModel]] = {
    "power": PowerData,
    "cadence": CadenceData,
    "heart_rate": HeartRateData,
    "speed": SpeedData,
    "distance": DistanceData,
    "device_connected": DeviceConnectedData,
    "device_disconnected": DeviceDisconnectedData,
    "device_capabilities": DeviceCapabilitiesData,
    "control_acquired": ControlAcquiredData,
    "control_released": ControlReleasedData,
    "target_power_set": TargetPowerSetData,
    "cadence_bailout_engaged": CadenceBailoutEngagedData,
    "cadence_bailout_disengaged": CadenceBailoutDisengagedData,
    "session_start": SessionStartData,
    "session_end": SessionEndData,
    "rider_annotation": RiderAnnotationData,
}


class Envelope(BaseModel):
    """Outer wrapper emitted on the wire for every event."""

    model_config = ConfigDict(extra="forbid")

    type: EventType
    ts: float
    session_id: UUID
    seq: Annotated[int, Field(ge=0)]
    device_kind: DeviceKind
    data: EventData

    @model_validator(mode="before")
    @classmethod
    def _coerce_data_to_typed_class(cls, values: object) -> object:
        """When parsing from a dict (e.g. ``model_validate_json``), use the
        envelope's ``type`` field to pick the matching data class. This makes
        round-trips type-stable even for data models whose shapes are
        identical (e.g. ``DeviceConnectedData`` and ``ControlAcquiredData``)."""
        if not isinstance(values, dict):
            return values
        type_ = values.get("type")
        data = values.get("data")
        if isinstance(data, dict) and isinstance(type_, str):
            expected = _DATA_BY_TYPE.get(type_)
            if expected is not None:
                values["data"] = expected.model_validate(data)
        return values

    def to_wire(self) -> str:
        # ``exclude_none`` keeps optional fields off the wire when unset.
        # Currently this matters for ``rider_annotation``'s ``note`` /
        # ``client_id`` / ``client_time_s`` / ``context`` — clients shouldn't
        # see explicit ``null``s for fields the rider didn't fill in. No
        # other event types use ``None`` defaults today, so the behavior is
        # narrow.
        return self.model_dump_json(exclude_none=True)


def now_ts() -> float:
    """Wall-clock seconds; chosen over monotonic so timestamps survive restarts."""
    return time.time()


# --- inbound command envelopes (engine → sidecar) -------------------------
#
# The WS connection is bidirectional: events flow sidecar→client (Envelope
# above) and commands flow client→sidecar (Command discriminated union below).
# Commands are validated against this schema; malformed messages are logged
# and dropped without affecting the event stream.


class SetTargetPowerCommand(BaseModel):
    """Set the trainer's ERG target wattage. Sidecar clamps to its configured
    safety bounds before issuing the FTMS opcode."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["set_target_power"]
    watts: int = Field(ge=-1000, le=5000)


class StartCommand(BaseModel):
    """Issue FTMS Start/Resume (opcode 0x07) — enter active workout state."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["start"]


class StopCommand(BaseModel):
    """Issue FTMS Stop/Pause (opcode 0x08, stop subcode 0x01)."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["stop"]


class ReleaseControlCommand(BaseModel):
    """Stop the workout and explicitly release the Control Point. Useful when
    the game wants to switch the trainer back to passive telemetry without
    disconnecting the BLE link."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["release_control"]


class AnnotateCommand(BaseModel):
    """Mark a moment on the event stream. Used by clients to capture rider
    intent or context that isn't otherwise visible in telemetry (e.g.
    F2-keypress bug reports, "too hard" tuning feedback, manual phase
    boundaries during debugging). The sidecar republishes the payload as
    a ``rider_annotation`` envelope with its own ``ts`` so timestamps
    stay monotonic with the rest of the stream. Not gated on
    ``--allow-trainer-control``: annotations never touch the trainer.

    See :class:`RiderAnnotationData` for the field semantics — this
    command mirrors that shape exactly."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["annotate"]
    tag: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=280)
    client_id: str | None = Field(default=None, max_length=64)
    client_time_s: float | None = Field(default=None, ge=0.0)
    context: dict[str, object] | None = Field(default=None)


Command = Annotated[
    SetTargetPowerCommand | StartCommand | StopCommand | ReleaseControlCommand | AnnotateCommand,
    Field(discriminator="type"),
]


ParsedCommand = (
    SetTargetPowerCommand | StartCommand | StopCommand | ReleaseControlCommand | AnnotateCommand
)


def parse_command_json(text: str) -> ParsedCommand:
    """Validate an incoming WS message as a Command. Raises ``ValidationError``
    on anything that isn't a recognized command shape."""
    from pydantic import TypeAdapter

    return TypeAdapter(Command).validate_json(text)
