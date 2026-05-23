"""JSONL recorder: one envelope per line, flushed per write.

Attached as an in-process ``EventBus`` subscriber when the CLI is launched
with ``--record <path>``. Replaces the external ``scripts/record_session.py``
WS subscriber — same JSONL output, one fewer process.

The bus replays its session-state snapshot to every new subscriber, so a
recording is self-contained even if recording starts mid-session (same
property the external script relied on).

The file is flushed on every write so a crash mid-ride leaves a usable
recording. Recordings WILL contain heart-rate samples and other personal
telemetry — keep the directory out of git.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from .ws_server import EventBus

log = logging.getLogger(__name__)


async def run_recorder(bus: EventBus, out_path: Path) -> None:
    """Subscribe to ``bus`` and write each envelope as one JSON line.

    Cancellation-safe: the file is closed via the context manager when the
    surrounding task is cancelled at shutdown."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("recording session to %s", out_path)
    with out_path.open("w", encoding="utf-8") as f:
        await _drain(bus, f)


async def _drain(bus: EventBus, f: TextIO) -> None:
    async with bus.subscribe() as queue:
        while True:
            env = await queue.get()
            f.write(env.to_wire())
            f.write("\n")
            f.flush()
