"""Round-trip + validation tests for the event schema."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from roguerglike_sidecar.events import (
    CadenceData,
    ControlAcquiredData,
    ControlReleasedData,
    DeviceCapabilitiesData,
    DeviceConnectedData,
    Envelope,
    HeartRateData,
    PowerData,
    ReleaseControlCommand,
    SetTargetPowerCommand,
    StartCommand,
    StopCommand,
    TargetPowerSetData,
    parse_command_json,
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


def test_device_capabilities_round_trip_with_target_power() -> None:
    env = _envelope(
        "device_capabilities",
        DeviceCapabilitiesData(
            kind="bike_trainer",
            name="KICKR CORE 1003",
            target_power=True,
            indoor_bike_simulation=True,
        ),
    )
    parsed = Envelope.model_validate_json(env.to_wire())
    assert parsed == env
    payload = json.loads(env.to_wire())["data"]
    assert payload["target_power"] is True
    assert payload["indoor_bike_simulation"] is True
    assert payload["target_resistance"] is False  # default for unset flags


def test_device_capabilities_defaults_are_all_false() -> None:
    """A device whose feature characteristic reports no targets supported
    serializes with all flags False — the engine can use this to gate ERG UI."""
    data = DeviceCapabilitiesData(kind="bike_trainer", name="Bare Trainer")
    assert data.target_power is False
    assert data.target_resistance is False
    assert data.target_inclination is False
    assert data.target_heart_rate is False
    assert data.indoor_bike_simulation is False


def test_control_acquired_round_trip() -> None:
    env = _envelope(
        "control_acquired",
        ControlAcquiredData(kind="bike_trainer", name="KICKR"),
    )
    parsed = Envelope.model_validate_json(env.to_wire())
    assert parsed == env


def test_control_released_includes_reason() -> None:
    env = _envelope(
        "control_released",
        ControlReleasedData(kind="bike_trainer", name="KICKR", reason="ws_disconnect_bailout"),
    )
    parsed = Envelope.model_validate_json(env.to_wire())
    assert parsed == env
    payload = json.loads(env.to_wire())
    assert payload["data"]["reason"] == "ws_disconnect_bailout"


def test_target_power_set_accepted() -> None:
    env = _envelope(
        "target_power_set",
        TargetPowerSetData(watts=235, accepted=True),
    )
    parsed = Envelope.model_validate_json(env.to_wire())
    assert parsed == env


def test_target_power_set_rejected_carries_reason() -> None:
    env = _envelope(
        "target_power_set",
        TargetPowerSetData(watts=9999, accepted=False, reason="trainer rejected (0x03)"),
    )
    parsed = Envelope.model_validate_json(env.to_wire())
    assert parsed.data.accepted is False  # type: ignore[union-attr]
    assert "0x03" in parsed.data.reason  # type: ignore[union-attr]


# --- inbound command envelope parsing ------------------------------------


def test_parse_set_target_power_command() -> None:
    cmd = parse_command_json('{"type": "set_target_power", "watts": 235}')
    assert isinstance(cmd, SetTargetPowerCommand)
    assert cmd.watts == 235


def test_parse_start_stop_release_commands_have_no_payload() -> None:
    assert isinstance(parse_command_json('{"type": "start"}'), StartCommand)
    assert isinstance(parse_command_json('{"type": "stop"}'), StopCommand)
    assert isinstance(parse_command_json('{"type": "release_control"}'), ReleaseControlCommand)


def test_parse_unknown_command_type_is_validation_error() -> None:
    with pytest.raises(ValidationError):
        parse_command_json('{"type": "set_inclination", "percent": 10}')


def test_parse_set_target_power_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        parse_command_json('{"type": "set_target_power", "watts": 200, "secret": true}')


def test_parse_set_target_power_bounds() -> None:
    # Schema bounds the inbound value to a wide range; the sidecar's safety
    # flags clamp further before any opcode reaches the trainer.
    with pytest.raises(ValidationError):
        parse_command_json('{"type": "set_target_power", "watts": 10000}')
    with pytest.raises(ValidationError):
        parse_command_json('{"type": "set_target_power", "watts": -5000}')


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
