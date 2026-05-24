"""Decode FTMS Indoor Bike Data packets against handcrafted fixtures.

Packet layout per Bluetooth SIG FTMS spec v1.0 §4.9: 2-byte flags field
followed by present fields in fixed order. Each fixture below states which
flag bits are set, what fields the layout therefore contains, and the
expected events the decoder must yield.
"""

from __future__ import annotations

from roguerglike_sidecar.ble.ftms_bike import (
    FtmsBikeProfile,
    parse_target_setting_features,
)
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


def test_decoder_extracts_distance_with_trainer_source() -> None:
    # flags=0x0054: speed (bit 0 clear), cadence (bit 2), distance (bit 4),
    # power (bit 6). All four fields are decoded; distance emits with
    # meters_delta=0 (the per-connection BleSource computes the real delta
    # against its tracked last_total before publishing).
    # speed 30.0 -> 3000 -> b'\xb8\x0b'
    # cadence 90 -> 180 -> b'\xb4\x00'
    # distance 12500 m -> b'\xd4\x30\x00' (u24 LE)
    # power 200 -> b'\xc8\x00'
    from roguerglike_sidecar.events import DistanceData

    events = _decode(b"\x54\x00\xb8\x0b\xb4\x00\xd4\x30\x00\xc8\x00")
    assert events == [
        ("speed", SpeedData(kph=30.0)),
        ("cadence", CadenceData(rpm=90)),
        ("distance", DistanceData(meters_total=12500.0, meters_delta=0.0, source="trainer")),
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
    # Capability discovery is opted into via these two attributes.
    assert p.feature_char_uuid is not None
    assert p.feature_char_uuid.endswith("00805f9b34fb")


# --- FTMS Fitness Machine Feature characteristic (0x2ACC) ----------------
#
# 8 bytes total: lower u32 = sensor feature bitmap (irrelevant to capabilities
# in our schema); upper u32 = target setting feature bitmap. The decoder only
# looks at the upper u32 and the schema-modelled subset of bits in it.


def _feature_payload(target_bits: int, sensor_bits: int = 0) -> bytes:
    """Build a synthetic 8-byte FTMS Feature characteristic payload."""
    return sensor_bits.to_bytes(4, "little") + target_bits.to_bytes(4, "little")


def test_features_all_zero_means_no_capabilities() -> None:
    assert parse_target_setting_features(_feature_payload(0x00000000)) == {
        "target_power": False,
        "target_resistance": False,
        "target_inclination": False,
        "target_heart_rate": False,
        "indoor_bike_simulation": False,
    }


def test_features_power_target_only() -> None:
    # bit 3 = Power Target Setting Supported
    caps = parse_target_setting_features(_feature_payload(1 << 3))
    assert caps["target_power"] is True
    assert caps["target_resistance"] is False
    assert caps["indoor_bike_simulation"] is False


def test_features_kickr_like_power_plus_simulation() -> None:
    # bits 3 + 13: typical smart trainer profile (ERG + SIM).
    bits = (1 << 3) | (1 << 13)
    caps = parse_target_setting_features(_feature_payload(bits))
    assert caps["target_power"] is True
    assert caps["indoor_bike_simulation"] is True
    assert caps["target_resistance"] is False


def test_features_all_schema_flags_set() -> None:
    # bits 1 (inclination) + 2 (resistance) + 3 (power) + 4 (HR) + 13 (sim).
    bits = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 13)
    caps = parse_target_setting_features(_feature_payload(bits))
    assert all(caps.values())


def test_features_sensor_bits_do_not_leak_into_target_caps() -> None:
    # Lower u32 set to bits that map to schema flags in the *target* table;
    # the parser must ignore them.
    caps = parse_target_setting_features(_feature_payload(0x00000000, sensor_bits=0xFFFFFFFF))
    assert all(v is False for v in caps.values())


def test_features_reserved_bits_are_ignored() -> None:
    # Bits 17-31 are reserved; they shouldn't affect any modelled flag.
    bits = 0xFFFE0000  # all reserved bits set, no modelled bits
    caps = parse_target_setting_features(_feature_payload(bits))
    assert all(v is False for v in caps.values())


def test_features_truncated_payload_yields_safe_defaults() -> None:
    # Some trainers omit or return shorter feature payloads. Decoder must be
    # total — return all False rather than raise.
    assert parse_target_setting_features(b"") == {
        "target_power": False,
        "target_resistance": False,
        "target_inclination": False,
        "target_heart_rate": False,
        "indoor_bike_simulation": False,
    }
    assert parse_target_setting_features(b"\x00\x00\x00\x00") == {
        "target_power": False,
        "target_resistance": False,
        "target_inclination": False,
        "target_heart_rate": False,
        "indoor_bike_simulation": False,
    }


def test_profile_parse_features_delegates_to_module_function() -> None:
    # The profile method is a thin wrapper; one fixture confirms the wiring.
    profile = FtmsBikeProfile()
    bits = (1 << 3) | (1 << 13)
    assert profile.parse_features(_feature_payload(bits))["target_power"] is True
