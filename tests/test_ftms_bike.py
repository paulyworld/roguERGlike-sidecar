"""Decode FTMS Indoor Bike Data packets against handcrafted fixtures.

Packet layout per Bluetooth SIG FTMS spec v1.0 §4.9: 2-byte flags field
followed by present fields in fixed order. Each fixture below states which
flag bits are set, what fields the layout therefore contains, and the
expected events the decoder must yield.
"""

from __future__ import annotations

from roguerglike_sidecar.ble.ftms_bike import FtmsBikeProfile
from roguerglike_sidecar.events import (
    CadenceData,
    EventData,
    EventType,
    HeartRateData,
    PowerData,
    SpeedData,
)


def _decode(packet: bytes) -> list[tuple[EventType, EventData]]:
    return list(FtmsBikeProfile().decode(packet))


def test_power_only_packet() -> None:
    # flags=0x0041: bit 0 set (instantaneous speed OMITTED), bit 6 set (power).
    # power=247W: struct '<h' = b'\xf7\x00'
    events = _decode(b"\x41\x00\xf7\x00")
    assert events == [("power", PowerData(watts=247))]


def test_full_bike_packet_with_speed_cadence_power_hr() -> None:
    # flags=0x0244: bit 0 clear (speed present), bit 2 (cadence), bit 6
    # (power), bit 9 (HR).
    # speed 32.4 kph -> 3240 -> b'\xa8\x0c'
    # cadence 88 rpm -> 176 (*2) -> b'\xb0\x00'
    # power 247 W -> b'\xf7\x00'
    # hr 142 bpm -> b'\x8e'
    events = _decode(b"\x44\x02\xa8\x0c\xb0\x00\xf7\x00\x8e")
    assert events == [
        ("speed", SpeedData(kph=32.4)),
        ("cadence", CadenceData(rpm=88)),
        ("power", PowerData(watts=247)),
        ("heart_rate", HeartRateData(bpm=142)),
    ]


def test_unsupported_fields_are_skipped_not_misaligned() -> None:
    # flags=0x0054: speed (bit 0 clear), cadence (bit 2), distance (bit 4),
    # power (bit 6). The decoder doesn't emit a distance event yet but must
    # still step over its 3 bytes so the power field decodes correctly.
    # speed 30.0 -> 3000 -> b'\xb8\x0b'
    # cadence 90 -> 180 -> b'\xb4\x00'
    # distance 12500 m -> b'\xd4\x30\x00' (u24 LE)
    # power 200 -> b'\xc8\x00'
    events = _decode(b"\x54\x00\xb8\x0b\xb4\x00\xd4\x30\x00\xc8\x00")
    assert events == [
        ("speed", SpeedData(kph=30.0)),
        ("cadence", CadenceData(rpm=90)),
        ("power", PowerData(watts=200)),
    ]


def test_negative_power_is_clamped_to_zero() -> None:
    # Some trainers emit small negative wattages from regen / measurement
    # noise. Our schema bounds power to [0, 3000]; clamp instead of dropping
    # the event so the live stream doesn't go silent.
    # flags=0x41 (power-only); power=-50 -> struct '<h' -> b'\xce\xff'
    events = _decode(b"\x41\x00\xce\xff")
    assert events == [("power", PowerData(watts=0))]


def test_zero_heart_rate_is_dropped() -> None:
    # FTMS uses bpm=0 to mean "no reading"; emitting a 0-bpm event would lie.
    # flags=0x0201: bit 0 set (no speed), bit 9 set (HR).
    events = _decode(b"\x01\x02\x00")
    assert events == []


def test_empty_payload_yields_nothing() -> None:
    assert _decode(b"") == []


def test_truncated_flags_yields_nothing() -> None:
    assert _decode(b"\x41") == []


def test_flags_claim_field_but_payload_truncated_yields_nothing() -> None:
    # flags=0x41 (power claimed) but only 1 byte of payload after flags.
    assert _decode(b"\x41\x00\xf7") == []


def test_profile_metadata() -> None:
    p = FtmsBikeProfile()
    assert p.name == "FTMS Bike"
    assert p.device_kind == "bike_trainer"
    assert p.service_uuid.endswith("00805f9b34fb")
    assert p.char_uuid.endswith("00805f9b34fb")
