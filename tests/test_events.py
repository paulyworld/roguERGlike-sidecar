"""Round-trip + validation tests for the event schema."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from roguerglike_sidecar.events import (
    CadenceData,
    DeviceConnectedData,
    Envelope,
    HeartRateData,
    PowerData,
)


def _envelope(type_: str, data: object) -> Envelope:
    return Envelope(
        type=type_,  # type: ignore[arg-type]
        ts=1715500000.0,
        session_id=uuid4(),
        seq=0,
        device_kind="mock",
        data=data,  # type: ignore[arg-type]
    )


def test_power_envelope_round_trip() -> None:
    env = _envelope("power", PowerData(watts=247))
    parsed = Envelope.model_validate_json(env.to_wire())
    assert parsed == env
    assert json.loads(env.to_wire())["data"] == {"watts": 247}


def test_cadence_and_heart_rate_round_trip() -> None:
    a = _envelope("cadence", CadenceData(rpm=88))
    b = _envelope("heart_rate", HeartRateData(bpm=142))
    assert Envelope.model_validate_json(a.to_wire()) == a
    assert Envelope.model_validate_json(b.to_wire()) == b


def test_device_connected_uses_device_kind_enum() -> None:
    env = _envelope(
        "device_connected",
        DeviceConnectedData(kind="bike_trainer", name="KICKR CORE 8B2A"),
    )
    payload = json.loads(env.to_wire())
    assert payload["data"]["kind"] == "bike_trainer"


def test_power_rejects_out_of_range() -> None:
    with pytest.raises(ValidationError):
        PowerData(watts=-1)
    with pytest.raises(ValidationError):
        PowerData(watts=10_000)


def test_envelope_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Envelope(
            type="power",
            ts=1.0,
            session_id=uuid4(),
            seq=0,
            device_kind="mock",
            data=PowerData(watts=100),
            extra_field="nope",  # type: ignore[call-arg]
        )
