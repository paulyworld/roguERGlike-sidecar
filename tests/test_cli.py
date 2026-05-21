"""Pure-logic CLI helpers (no Click invocation, no BLE)."""

from __future__ import annotations

from roguerglike_sidecar.ble.scan import DiscoveredDevice
from roguerglike_sidecar.cli import _match_device


def _devs() -> list[DiscoveredDevice]:
    return [
        DiscoveredDevice(address="AA:BB:CC:11:22:33", name="KICKR CORE 8B2A", rssi=-45),
        DiscoveredDevice(address="DD:EE:FF:44:55:66", name="Wahoo HRM 1234", rssi=-60),
    ]


def test_match_by_full_address() -> None:
    matched = _match_device(_devs(), "aa:bb:cc:11:22:33")
    assert matched is not None and matched.name == "KICKR CORE 8B2A"


def test_match_by_full_name_case_insensitive() -> None:
    matched = _match_device(_devs(), "kickr core 8b2a")
    assert matched is not None and matched.name == "KICKR CORE 8B2A"


def test_match_by_name_substring() -> None:
    matched = _match_device(_devs(), "kickr")
    assert matched is not None and matched.address == "AA:BB:CC:11:22:33"


def test_no_match_returns_none() -> None:
    assert _match_device(_devs(), "tacx") is None


def test_empty_query_returns_none() -> None:
    # An empty needle would otherwise substring-match every name; ensure we
    # don't auto-pair to a random device because the user passed --device-bike "".
    assert _match_device(_devs(), "") is None
