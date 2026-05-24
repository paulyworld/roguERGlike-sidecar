"""Export completed ride recordings to standard activity formats.

The sidecar's JSONL recording is the durable source of truth. Export
modules read it and produce the formats fitness platforms ingest —
``.fit`` first (broadest compatibility, Strava direct upload, native
TrainingPeaks). ``.tcx`` / ``.gpx`` may follow if real use surfaces
import problems.
"""
