"""Pure-logic CLI helpers (no Click invocation, no BLE)."""

from __future__ import annotations

from roguerglike_sidecar.ble.scan import DiscoveredDevice
from roguerglike_sidecar.cli import _match_device, _resolve_drop_types, _resolve_matches


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


# HR de-dup policy resolution. The bike's FTMS packet may embed HR; when a
# standalone HR sensor is also paired, exactly one source must publish
# heart_rate events. ``_resolve_drop_types`` decides which.


def test_drop_types_bike_only_no_suppression() -> None:
    bike, hr = _resolve_drop_types(has_bike=True, has_hr=False, prefer_bike_hr=False)
    assert bike == frozenset() and hr == frozenset()


def test_drop_types_hr_only_no_suppression() -> None:
    bike, hr = _resolve_drop_types(has_bike=False, has_hr=True, prefer_bike_hr=False)
    assert bike == frozenset() and hr == frozenset()


def test_drop_types_both_default_prefers_strap() -> None:
    """Default policy: standalone strap wins; bike drops its embedded HR."""
    bike, hr = _resolve_drop_types(has_bike=True, has_hr=True, prefer_bike_hr=False)
    assert bike == frozenset({"heart_rate"}) and hr == frozenset()


def test_drop_types_both_with_prefer_bike_hr_inverts() -> None:
    bike, hr = _resolve_drop_types(has_bike=True, has_hr=True, prefer_bike_hr=True)
    assert bike == frozenset() and hr == frozenset({"heart_rate"})


def test_drop_types_neither_paired_no_suppression() -> None:
    """Defensive: the CLI rejects this case before calling, but the resolver
    must still be sane if it's ever called with no sources."""
    bike, hr = _resolve_drop_types(has_bike=False, has_hr=False, prefer_bike_hr=False)
    assert bike == frozenset() and hr == frozenset()


# --- scan-retry partial matching (`_resolve_matches`) ---------------------
#
# Forgiving scan loop in _run_live re-scans until all queries match, printing
# what's still missing between cycles. The pure resolution between scan
# results and queries is testable in isolation.


def _scan_with(
    *devices_by_profile: tuple[str, list[DiscoveredDevice]],
) -> dict[str, list[DiscoveredDevice]]:
    return dict(devices_by_profile)


def test_resolve_matches_all_present() -> None:
    discovered = _scan_with(
        ("FTMS Bike", [DiscoveredDevice("AA:BB", "KICKR CORE 8B2A", -50)]),
        ("HR Sensor", [DiscoveredDevice("CC:DD", "MUDRAT-DETECTOR", -65)]),
    )
    matched, missing = _resolve_matches(
        discovered,
        {"FTMS Bike": "KICKR", "HR Sensor": "mudrat"},
    )
    assert missing == []
    assert matched["FTMS Bike"].name == "KICKR CORE 8B2A"
    assert matched["HR Sensor"].name == "MUDRAT-DETECTOR"


def test_resolve_matches_reports_missing_profiles() -> None:
    """A scan cycle that produces no devices for one profile should leave
    that profile's query in ``missing`` so the retry loop knows what to
    keep waiting for."""
    discovered = _scan_with(
        ("FTMS Bike", [DiscoveredDevice("AA:BB", "KICKR CORE 8B2A", -50)]),
        ("HR Sensor", []),  # Whoop didn't show up yet
    )
    matched, missing = _resolve_matches(
        discovered,
        {"FTMS Bike": "KICKR", "HR Sensor": "mudrat"},
    )
    assert missing == ["HR Sensor"]
    assert "FTMS Bike" in matched and "HR Sensor" not in matched


def test_resolve_matches_skips_already_satisfied() -> None:
    """Queries already matched from a previous cycle stay matched even if
    the new scan happens not to include the device (advertisement gap).
    Avoids re-failing on a device we already know is there."""
    discovered = _scan_with(
        ("FTMS Bike", []),  # KICKR briefly missed this advertisement cycle
        ("HR Sensor", [DiscoveredDevice("CC:DD", "MUDRAT-DETECTOR", -65)]),
    )
    already = {"FTMS Bike": DiscoveredDevice("AA:BB", "KICKR CORE 8B2A", -55)}
    matched, missing = _resolve_matches(
        discovered,
        {"FTMS Bike": "KICKR", "HR Sensor": "mudrat"},
        already=already,
    )
    assert missing == []
    assert matched["FTMS Bike"].address == "AA:BB"  # carried over from `already`
    assert matched["HR Sensor"].name == "MUDRAT-DETECTOR"


def test_resolve_matches_query_substring_against_scan_results() -> None:
    """``_resolve_matches`` defers to ``_match_device`` for each query, which
    falls back to substring matching on names. So a query like 'mudrat'
    finds 'MUDRAT-DETECTOR'."""
    discovered = _scan_with(
        ("HR Sensor", [DiscoveredDevice("CC:DD", "MUDRAT-DETECTOR", -65)]),
    )
    matched, missing = _resolve_matches(discovered, {"HR Sensor": "mudrat"})
    assert missing == []
    assert matched["HR Sensor"].name == "MUDRAT-DETECTOR"
