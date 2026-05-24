"""JSONL → FIT activity file converter.

Reads a sidecar JSONL recording (produced by ``--record``), reconstructs
a per-second record stream, and writes a FIT activity that Strava,
TrainingPeaks, and other major platforms ingest as an indoor / virtual
trainer ride.

What gets into the FIT:

- One ``RecordMessage`` per second of the recording, populated with
  whichever fields the JSONL had at that moment: ``power``, ``cadence``,
  ``heart_rate``, ``speed``, ``distance``.
- One ``LapMessage`` covering the whole ride (no lap structure in the
  sidecar yet — adding later if rides grow lap markers).
- One ``SessionMessage`` with sport=CYCLING, sub_sport=INDOOR_CYCLING
  so platforms classify the upload as a trainer ride rather than an
  outdoor route.
- One ``ActivityMessage`` wrapping it all.

What's NOT in the FIT:

- ``rider_annotation`` envelopes — FIT has no native rider-note channel
  that Strava/TP surface meaningfully. They stay in the JSONL for
  post-ride analyzers to consume; future TCX export could embed them as
  notes.
- Synthetic vs trainer-reported distance source attribution — FIT
  doesn't have a standard field for this. The export currently passes
  whatever ``distance`` events are in the JSONL through verbatim; the
  rider should know whether the session was ERG-only (trainer distance)
  or terrain-mode (synthetic). A future export could write a developer
  field with the source, but consumer platforms wouldn't see it.

See :func:`build_fit` for the API.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import (
    Event,
    EventType,
    FileType,
    Manufacturer,
    Sport,
    SubSport,
)

log = logging.getLogger(__name__)


@dataclass
class _Sample:
    """Aggregated per-second snapshot from the JSONL stream."""

    ts_ms: int
    power: int | None = None
    cadence: int | None = None
    heart_rate: int | None = None
    speed_mps: float | None = None
    distance_m: float | None = None


def _read_jsonl_lines(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                log.warning("skipping malformed line in %s: %s", path, e)


_TELEMETRY_TYPES = frozenset({"power", "cadence", "heart_rate", "speed", "distance"})


def _bucket_to_samples(events: Iterable[dict[str, Any]]) -> list[_Sample]:
    """Bucket the JSONL event stream into per-second samples.

    Sidecar telemetry runs at ~1 Hz already, so each bucket usually
    holds one of each type. If multiple of a kind arrive in the same
    second (e.g. high-rate cadence from a smart trainer that pushes
    fast), the latest value within the second wins — close enough for
    a FIT record stream.

    Non-telemetry events (session_start, device_connected, paused,
    rider_annotation, hello, ...) don't get a bucket — they're not part
    of the record stream and would otherwise produce empty records at
    the start/edges of the file.
    """
    buckets: dict[int, _Sample] = {}
    for env in events:
        ts = env.get("ts")
        type_ = env.get("type")
        if not isinstance(ts, int | float) or type_ not in _TELEMETRY_TYPES:
            continue
        bucket_s = int(ts)
        sample = buckets.setdefault(bucket_s, _Sample(ts_ms=bucket_s * 1000))
        data = env.get("data") or {}
        if type_ == "power":
            sample.power = int(data.get("watts", 0))
        elif type_ == "cadence":
            sample.cadence = int(data.get("rpm", 0))
        elif type_ == "heart_rate":
            sample.heart_rate = int(data.get("bpm", 0))
        elif type_ == "speed":
            kph = float(data.get("kph", 0.0))
            sample.speed_mps = kph / 3.6
        elif type_ == "distance":
            sample.distance_m = float(data.get("meters_total", 0.0))
    return [buckets[s] for s in sorted(buckets)]


def _build_records(samples: list[_Sample]) -> list[RecordMessage]:
    out: list[RecordMessage] = []
    for s in samples:
        rec = RecordMessage()
        rec.timestamp = s.ts_ms
        if s.power is not None:
            rec.power = s.power
        if s.cadence is not None:
            rec.cadence = s.cadence
        if s.heart_rate is not None:
            rec.heart_rate = s.heart_rate
        if s.speed_mps is not None:
            rec.speed = s.speed_mps
        if s.distance_m is not None:
            rec.distance = s.distance_m
        out.append(rec)
    return out


def _summary(samples: list[_Sample]) -> dict[str, Any]:
    """Aggregate per-session totals/averages for the SessionMessage."""
    powers = [s.power for s in samples if s.power is not None]
    cadences = [s.cadence for s in samples if s.cadence is not None]
    hrs = [s.heart_rate for s in samples if s.heart_rate is not None]
    distances = [s.distance_m for s in samples if s.distance_m is not None]
    speeds = [s.speed_mps for s in samples if s.speed_mps is not None]

    def _avg(xs: list[float] | list[int]) -> float | None:
        return sum(xs) / len(xs) if xs else None

    def _max(xs: list[float] | list[int]) -> float | None:
        return max(xs) if xs else None

    return {
        "avg_power": _avg(powers),
        "max_power": _max(powers),
        "avg_cadence": _avg(cadences),
        "max_cadence": _max(cadences),
        "avg_heart_rate": _avg(hrs),
        "max_heart_rate": _max(hrs),
        "avg_speed": _avg(speeds),
        "max_speed": _max(speeds),
        "total_distance": distances[-1] if distances else 0.0,
    }


def build_fit(jsonl_path: Path, fit_path: Path) -> None:
    """Convert a JSONL recording at ``jsonl_path`` into a FIT activity at
    ``fit_path``.

    Sport=CYCLING, sub_sport=INDOOR_CYCLING — Strava, TrainingPeaks,
    and most platforms read this as a trainer/indoor ride. If the FIT
    has zero records (empty / corrupt JSONL), raises ``ValueError``."""
    events = list(_read_jsonl_lines(jsonl_path))
    samples = _bucket_to_samples(events)
    if not samples:
        raise ValueError(f"no telemetry samples found in {jsonl_path}")

    start_ms = samples[0].ts_ms
    end_ms = samples[-1].ts_ms
    total_elapsed_s = max(1.0, (end_ms - start_ms) / 1000.0)
    summary = _summary(samples)

    builder = FitFileBuilder(auto_define=True, min_string_size=50)

    # File-id message — identifies the file as a FIT activity from us.
    file_id = FileIdMessage()
    file_id.type = FileType.ACTIVITY
    file_id.manufacturer = Manufacturer.DEVELOPMENT.value
    file_id.product = 0
    file_id.time_created = start_ms
    file_id.serial_number = 0x12345678
    builder.add(file_id)

    # Workout start event.
    start_event = EventMessage()
    start_event.timestamp = start_ms
    start_event.event = Event.TIMER
    start_event.event_type = EventType.START
    builder.add(start_event)

    # Per-second records.
    builder.add_all(_build_records(samples))

    # Workout stop event.
    stop_event = EventMessage()
    stop_event.timestamp = end_ms
    stop_event.event = Event.TIMER
    stop_event.event_type = EventType.STOP_ALL
    builder.add(stop_event)

    # One lap covering the whole ride.
    lap = LapMessage()
    lap.timestamp = end_ms
    lap.start_time = start_ms
    lap.total_elapsed_time = total_elapsed_s
    lap.total_timer_time = total_elapsed_s
    if summary["total_distance"]:
        lap.total_distance = summary["total_distance"]
    if summary["avg_power"] is not None:
        lap.avg_power = int(round(summary["avg_power"]))
        lap.max_power = int(round(summary["max_power"] or 0))
    if summary["avg_cadence"] is not None:
        lap.avg_cadence = int(round(summary["avg_cadence"]))
        lap.max_cadence = int(round(summary["max_cadence"] or 0))
    if summary["avg_heart_rate"] is not None:
        lap.avg_heart_rate = int(round(summary["avg_heart_rate"]))
        lap.max_heart_rate = int(round(summary["max_heart_rate"] or 0))
    builder.add(lap)

    # Session summary — sport/sub_sport are the indoor/trainer markers
    # Strava and others read.
    session = SessionMessage()
    session.timestamp = end_ms
    session.start_time = start_ms
    session.total_elapsed_time = total_elapsed_s
    session.total_timer_time = total_elapsed_s
    session.sport = Sport.CYCLING
    session.sub_sport = SubSport.INDOOR_CYCLING
    if summary["total_distance"]:
        session.total_distance = summary["total_distance"]
    if summary["avg_power"] is not None:
        session.avg_power = int(round(summary["avg_power"]))
        session.max_power = int(round(summary["max_power"] or 0))
    if summary["avg_cadence"] is not None:
        session.avg_cadence = int(round(summary["avg_cadence"]))
        session.max_cadence = int(round(summary["max_cadence"] or 0))
    if summary["avg_heart_rate"] is not None:
        session.avg_heart_rate = int(round(summary["avg_heart_rate"]))
        session.max_heart_rate = int(round(summary["max_heart_rate"] or 0))
    session.num_laps = 1
    builder.add(session)

    # Activity wrapper.
    activity = ActivityMessage()
    activity.timestamp = end_ms
    activity.total_timer_time = total_elapsed_s
    activity.num_sessions = 1
    builder.add(activity)

    fit_file = builder.build()
    fit_file.to_file(str(fit_path))
    log.info(
        "wrote %d-record FIT to %s (%.0fs elapsed, %.0f m, %d W avg)",
        len(samples),
        fit_path,
        total_elapsed_s,
        summary["total_distance"] or 0.0,
        int(round(summary["avg_power"] or 0)),
    )
