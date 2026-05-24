"""JSONL → FIT export tests.

Validates that a recording can be turned into a syntactically-valid FIT
activity that fitness platforms will accept. Read-back uses fit-tool's
parser so the assertions cover round-trip integrity, not just "did the
function not crash."
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fit_tool.fit_file import FitFile
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import Sport, SubSport

from roguerglike_sidecar.export.fit import build_fit


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line))
            f.write("\n")


def _minimal_session_events(start_ts: float, n_ticks: int) -> list[dict]:
    """Build a JSONL representing ``n_ticks`` seconds of basic telemetry.
    One power/cadence/heart_rate/speed/distance event per second, increasing
    distance, steady-ish other values."""
    out: list[dict] = [
        {
            "type": "session_start",
            "ts": start_ts,
            "session_id": "00000000-0000-0000-0000-000000000000",
            "seq": 0,
            "device_kind": "bike_trainer",
            "data": {"session_id": "00000000-0000-0000-0000-000000000000"},
        }
    ]
    seq = 1
    distance = 0.0
    for i in range(n_ticks):
        ts = start_ts + i + 1
        distance += 8.0  # ~28.8 kph at 1Hz
        for type_, data in (
            ("power", {"watts": 200}),
            ("cadence", {"rpm": 88}),
            ("heart_rate", {"bpm": 140}),
            ("speed", {"kph": 28.8}),
            (
                "distance",
                {"meters_total": distance, "meters_delta": 8.0, "source": "trainer"},
            ),
        ):
            out.append(
                {
                    "type": type_,
                    "ts": ts,
                    "session_id": "00000000-0000-0000-0000-000000000000",
                    "seq": seq,
                    "device_kind": "bike_trainer",
                    "data": data,
                }
            )
            seq += 1
    return out


def test_build_fit_produces_readable_activity(tmp_path: Path) -> None:
    """Round-trip: write JSONL → build_fit → parse the FIT → assert messages
    exist with the right counts and indoor classification."""
    jsonl = tmp_path / "ride.jsonl"
    fit_out = tmp_path / "ride.fit"
    _write_jsonl(jsonl, _minimal_session_events(start_ts=1715500000.0, n_ticks=60))

    build_fit(jsonl, fit_out)

    parsed = FitFile.from_file(str(fit_out))
    records = [m for m in parsed.records if isinstance(m.message, RecordMessage)]
    sessions = [m for m in parsed.records if isinstance(m.message, SessionMessage)]
    laps = [m for m in parsed.records if isinstance(m.message, LapMessage)]

    # 60 per-second buckets → 60 records.
    assert len(records) == 60
    assert len(sessions) == 1
    assert len(laps) == 1

    session = sessions[0].message
    # Indoor / trainer classification — what platforms key off.
    assert session.sport == Sport.CYCLING.value
    assert session.sub_sport == SubSport.INDOOR_CYCLING.value
    # Distance reflects what's in the JSONL (60s × 8m = 480m).
    assert session.total_distance == pytest.approx(480.0, abs=1.0)
    # Avg power matches the constant 200 W we wrote.
    assert session.avg_power == 200


def test_build_fit_handles_partial_telemetry(tmp_path: Path) -> None:
    """JSONL with only power events (no cadence/HR/speed/distance) still
    produces a valid FIT — records just have fewer populated fields."""
    jsonl = tmp_path / "power-only.jsonl"
    fit_out = tmp_path / "power-only.fit"
    events = [
        {
            "type": "power",
            "ts": 1715500000.0 + i,
            "session_id": "00000000-0000-0000-0000-000000000000",
            "seq": i,
            "device_kind": "bike_trainer",
            "data": {"watts": 150 + i},
        }
        for i in range(10)
    ]
    _write_jsonl(jsonl, events)

    build_fit(jsonl, fit_out)

    parsed = FitFile.from_file(str(fit_out))
    records = [m for m in parsed.records if isinstance(m.message, RecordMessage)]
    assert len(records) == 10


def test_build_fit_raises_on_empty_jsonl(tmp_path: Path) -> None:
    """Empty / corrupt recordings should fail loudly so the user knows the
    export didn't silently produce a junk file."""
    jsonl = tmp_path / "empty.jsonl"
    jsonl.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="no telemetry samples"):
        build_fit(jsonl, tmp_path / "out.fit")


def test_build_fit_skips_malformed_lines(tmp_path: Path) -> None:
    """A line that fails JSON parsing should be skipped, not abort the export
    (recording could include a partial last line on hard kill)."""
    jsonl = tmp_path / "mixed.jsonl"
    valid = _minimal_session_events(start_ts=1715500000.0, n_ticks=5)
    with jsonl.open("w", encoding="utf-8") as f:
        for line in valid:
            f.write(json.dumps(line))
            f.write("\n")
        f.write("{this is not valid json\n")

    build_fit(jsonl, tmp_path / "mixed.fit")  # should not raise
