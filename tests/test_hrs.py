"""Decode Heart Rate Service measurement packets against handcrafted fixtures.

Packet layout per Bluetooth SIG HRS spec v1.0 §3.1: 1-byte flags then a u8 or
u16 HR value (per flags bit 0), optional 2-byte energy expended (bit 3),
optional repeating 2-byte RR-interval values (bit 4) filling the rest. Each
fixture below states the flag bits, packet contents, and expected events.
"""

from __future__ import annotations

from roguerglike_sidecar.ble.hrs import HrsProfile
from roguerglike_sidecar.events import (
    EventData,
    EventType,
    HeartRateData,
)


def _decode(packet: bytes) -> list[tuple[EventType, EventData]]:
    return list(HrsProfile().decode(packet))


def test_u8_heart_rate_simple() -> None:
    # flags=0x00 (u8 HR, no extras); bpm=75 (0x4B).
    assert _decode(b"\x00\x4b") == [("heart_rate", HeartRateData(bpm=75))]


def test_u16_heart_rate() -> None:
    # flags=0x01 (u16 HR); bpm=176 = 0x00B0 little-endian.
    assert _decode(b"\x01\xb0\x00") == [("heart_rate", HeartRateData(bpm=176))]


def test_sensor_contact_bits_do_not_gate_emission() -> None:
    # flags=0x06: bits 1-2 set ("supported, contact detected"). Spec-wise
    # informational only; we must not gate emission on it (some devices lie).
    assert _decode(b"\x06\x4b") == [("heart_rate", HeartRateData(bpm=75))]


def test_energy_expended_present_is_consumed_not_emitted() -> None:
    # flags=0x08: bit 3 set (energy expended u16 follows HR).
    # bpm=75; energy=0x03E8=1000 kJ — present but not in our schema yet.
    assert _decode(b"\x08\x4b\xe8\x03") == [("heart_rate", HeartRateData(bpm=75))]


def test_rr_intervals_present_are_consumed_not_emitted() -> None:
    # flags=0x10: bit 4 set (RR-intervals fill the remainder of the packet).
    # bpm=75; RR=1024 (0x0400), RR=1023 (0x03FF).
    assert _decode(b"\x10\x4b\x00\x04\xff\x03") == [("heart_rate", HeartRateData(bpm=75))]


def test_energy_and_rr_both_present() -> None:
    # flags=0x18: bits 3 + 4 set. bpm=75; energy=1000; RR=1024.
    assert _decode(b"\x18\x4b\xe8\x03\x00\x04") == [("heart_rate", HeartRateData(bpm=75))]


def test_zero_bpm_is_dropped() -> None:
    # HRS sentinel for "no reading" / "no contact".
    assert _decode(b"\x00\x00") == []


def test_implausibly_low_bpm_is_dropped() -> None:
    # Below schema bound (20). Probably sensor noise / drop contact.
    assert _decode(b"\x00\x0a") == []


def test_implausibly_high_bpm_is_dropped() -> None:
    # Above schema bound (240). Don't clamp — a wrong HR actively misleads.
    assert _decode(b"\x01\x2c\x01") == []  # 0x012C = 300


def test_u16_truncated_yields_nothing() -> None:
    # flags claim u16 HR (bit 0) but only 1 HR byte present after flags.
    assert _decode(b"\x01\x4b") == []


def test_energy_claimed_but_truncated_yields_nothing() -> None:
    # flags=0x08, HR=75, then energy claimed but only 1 of its 2 bytes present.
    assert _decode(b"\x08\x4b\xe8") == []


def test_empty_payload_yields_nothing() -> None:
    assert _decode(b"") == []


def test_one_byte_payload_yields_nothing() -> None:
    # Just a flags byte, no HR.
    assert _decode(b"\x00") == []


def test_profile_metadata() -> None:
    p = HrsProfile()
    assert p.name == "HR Sensor"
    assert p.device_kind == "hr_sensor"
    assert p.service_uuid.endswith("00805f9b34fb")
    assert p.char_uuid.endswith("00805f9b34fb")
