"""Round-trip + validation tests for the event schema."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from roguerglike_sidecar.events import (
    AnnotateCommand,
    CadenceData,
    ControlAcquiredData,
    ControlReleasedData,
    DeviceCapabilitiesData,
    DeviceConnectedData,
    Envelope,
    HeartRateData,
    HelloData,
    PauseCommand,
    PausedData,
    PowerData,
    ReleaseControlCommand,
    ResumeCommand,
    ResumedData,
    RiderAnnotationData,
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


def test_parse_annotate_command_minimal() -> None:
    cmd = parse_command_json('{"type": "annotate", "tag": "ui-pause"}')
    assert isinstance(cmd, AnnotateCommand)
    assert cmd.tag == "ui-pause"
    assert cmd.note is None
    assert cmd.client_id is None


def test_parse_annotate_command_full() -> None:
    cmd = parse_command_json(
        '{"type": "annotate", "tag": "felt-unfair", '
        '"note": "ramp came back too hot after the pause", '
        '"client_id": "concert-mvp"}'
    )
    assert isinstance(cmd, AnnotateCommand)
    assert cmd.tag == "felt-unfair"
    assert cmd.note == "ramp came back too hot after the pause"
    assert cmd.client_id == "concert-mvp"


def test_annotate_command_rejects_empty_tag() -> None:
    with pytest.raises(ValidationError):
        parse_command_json('{"type": "annotate", "tag": ""}')


def test_annotate_command_rejects_oversize_fields() -> None:
    # Tag cap at 64 chars (room for "ui-pause:between-rounds:round-3" style).
    with pytest.raises(ValidationError):
        parse_command_json(f'{{"type": "annotate", "tag": "{"x" * 65}"}}')
    # Note cap at 280 chars (tweet-sized — long enough for context, short
    # enough that it can't be mid-ride free-typing the whole session log).
    with pytest.raises(ValidationError):
        parse_command_json(f'{{"type": "annotate", "tag": "marker", "note": "{"x" * 281}"}}')


def test_rider_annotation_envelope_round_trip() -> None:
    env = Envelope(
        type="rider_annotation",
        ts=1715500000.0,
        session_id=uuid4(),
        seq=0,
        device_kind="client",
        data=RiderAnnotationData(tag="bug", note="UI froze for 2s", client_id="concert-mvp"),
    )
    parsed = Envelope.model_validate_json(env.to_wire())
    assert parsed == env
    assert isinstance(parsed.data, RiderAnnotationData)
    # Optional fields not set on construction should be absent from the wire,
    # not serialized as ``null``. Keeps recordings/replays compact and means
    # post-ride analyzers don't have to special-case explicit-null vs absent.
    assert json.loads(env.to_wire())["data"] == {
        "tag": "bug",
        "note": "UI froze for 2s",
        "client_id": "concert-mvp",
    }


def test_rider_annotation_envelope_with_client_time_and_context() -> None:
    """Full hybrid schema: a tuning-feedback annotation with ride-state context."""
    env = Envelope(
        type="rider_annotation",
        ts=1715500000.0,
        session_id=uuid4(),
        seq=0,
        device_kind="client",
        data=RiderAnnotationData(
            tag="too-hard",
            client_id="gizzERG",
            client_time_s=2412.5,
            context={
                "profile_id": "king-gizzard-night-2",
                "profile_version": "terrain-v0",
                "mode": "terrain_erg",
                "section": "Motor Spirit",
                "target_watts": 228,
                "power": 205,
                "cadence": 71,
                "hr": 154,
                "wkg": 2.93,
                "grade": 5.5,
                "hardware_source": "trainer_power",
            },
        ),
    )
    parsed = Envelope.model_validate_json(env.to_wire())
    assert parsed == env
    assert isinstance(parsed.data, RiderAnnotationData)
    assert parsed.data.client_time_s == 2412.5
    assert parsed.data.context is not None
    assert parsed.data.context["mode"] == "terrain_erg"
    assert parsed.data.context["target_watts"] == 228


def test_parse_annotate_command_with_client_time_and_context() -> None:
    cmd = parse_command_json(
        '{"type": "annotate", "tag": "too-hard", "client_time_s": 2412.5, '
        '"client_id": "gizzERG", '
        '"context": {"mode": "terrain_erg", "grade": 5.5, "target_watts": 228}}'
    )
    assert isinstance(cmd, AnnotateCommand)
    assert cmd.client_time_s == 2412.5
    assert cmd.context == {"mode": "terrain_erg", "grade": 5.5, "target_watts": 228}


def test_annotate_command_rejects_negative_client_time() -> None:
    with pytest.raises(ValidationError):
        parse_command_json('{"type": "annotate", "tag": "marker", "client_time_s": -1.0}')


def test_annotate_command_accepts_empty_context() -> None:
    """Empty dict is a valid context — represents 'no client state to share'.
    Distinct from absent context (the field omitted entirely)."""
    cmd = parse_command_json('{"type": "annotate", "tag": "marker", "context": {}}')
    assert isinstance(cmd, AnnotateCommand)
    assert cmd.context == {}


# --- hello envelope ------------------------------------------------------


def test_hello_envelope_round_trip() -> None:
    env = Envelope(
        type="hello",
        ts=1715500000.0,
        session_id=uuid4(),
        seq=0,
        device_kind="sidecar",
        data=HelloData(
            protocol_version="1.0.0",
            sidecar_version="0.1.0",
            features=["set_target_power", "recording", "annotations"],
            mode="live",
        ),
    )
    parsed = Envelope.model_validate_json(env.to_wire())
    assert parsed == env
    assert isinstance(parsed.data, HelloData)
    assert parsed.data.protocol_version == "1.0.0"
    assert "annotations" in parsed.data.features


def test_hello_rejects_unknown_mode() -> None:
    """Mode is a strict literal — typos in the launcher shouldn't get past
    construction. Tests the wire ingress path."""
    with pytest.raises(ValidationError):
        Envelope.model_validate_json(
            '{"type": "hello", "ts": 1.0, '
            '"session_id": "00000000-0000-0000-0000-000000000000", "seq": 0, '
            '"device_kind": "sidecar", "data": {"protocol_version": "1.0.0", '
            '"sidecar_version": "0.1.0", "features": [], "mode": "freestyle"}}'
        )


def test_hello_rejects_extra_fields() -> None:
    """Strict model — adding a field without a schema bump means the client
    sees a parse error rather than silently picking up unknown semantics."""
    with pytest.raises(ValidationError):
        HelloData(
            protocol_version="1.0.0",
            sidecar_version="0.1.0",
            features=[],
            mode="mock",
            uptime_s=120.0,  # type: ignore[call-arg]
        )


# --- structured pause (Pattern B): commands ----------------------------------


def test_parse_pause_command_minimal() -> None:
    cmd = parse_command_json('{"type": "pause"}')
    assert isinstance(cmd, PauseCommand)
    assert cmd.reason is None
    assert cmd.target_watts is None


def test_parse_pause_command_full() -> None:
    cmd = parse_command_json('{"type": "pause", "reason": "between-rounds", "target_watts": 75}')
    assert isinstance(cmd, PauseCommand)
    assert cmd.reason == "between-rounds"
    assert cmd.target_watts == 75


def test_parse_pause_rejects_negative_watts() -> None:
    with pytest.raises(ValidationError):
        parse_command_json('{"type": "pause", "target_watts": -50}')


def test_parse_pause_rejects_oversize_reason() -> None:
    too_long = "x" * 65
    with pytest.raises(ValidationError):
        parse_command_json(f'{{"type": "pause", "reason": "{too_long}"}}')


def test_parse_resume_command() -> None:
    cmd = parse_command_json('{"type": "resume"}')
    assert isinstance(cmd, ResumeCommand)


def test_parse_resume_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        parse_command_json('{"type": "resume", "force": true}')


# --- structured pause (Pattern B): envelopes ---------------------------------


def test_paused_envelope_round_trip() -> None:
    env = Envelope(
        type="paused",
        ts=1715500000.0,
        session_id=uuid4(),
        seq=0,
        device_kind="bike_trainer",
        data=PausedData(
            kind="bike_trainer",
            name="KICKR",
            reason="between-rounds",
            target_watts=75,
            previous_target_watts=210,
        ),
    )
    parsed = Envelope.model_validate_json(env.to_wire())
    assert parsed == env
    assert isinstance(parsed.data, PausedData)
    assert parsed.data.reason == "between-rounds"
    assert parsed.data.target_watts == 75
    assert parsed.data.previous_target_watts == 210


def test_resumed_envelope_round_trip() -> None:
    env = Envelope(
        type="resumed",
        ts=1715500000.0,
        session_id=uuid4(),
        seq=0,
        device_kind="bike_trainer",
        data=ResumedData(
            kind="bike_trainer",
            name="KICKR",
            restored_to_watts=210,
            ramped_over_s=3.0,
        ),
    )
    parsed = Envelope.model_validate_json(env.to_wire())
    assert parsed == env
    assert isinstance(parsed.data, ResumedData)
    assert parsed.data.restored_to_watts == 210


def test_paused_and_disengaged_have_distinct_event_types() -> None:
    """``paused`` (structured) and ``cadence_bailout_engaged`` (safety) are
    intentionally separate event types. Clients route them through
    different UI paths even though the underlying mechanism (target drop)
    is similar."""
    paused = Envelope(
        type="paused",
        ts=1.0,
        session_id=uuid4(),
        seq=0,
        device_kind="bike_trainer",
        data=PausedData(kind="bike_trainer", name="K", target_watts=75, previous_target_watts=200),
    )
    assert paused.type == "paused"
    assert paused.type != "cadence_bailout_engaged"
