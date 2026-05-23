"""JSONL recorder writes the bus stream verbatim, with session-state replay."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

from roguerglike_sidecar.events import (
    CadenceData,
    DeviceConnectedData,
    PowerData,
    SessionStartData,
)
from roguerglike_sidecar.recording import run_recorder
from roguerglike_sidecar.ws_server import EventBus


async def _drain_n_lines(path: Path, n: int, timeout_s: float = 1.0) -> list[dict]:
    """Poll the file until ``n`` non-empty lines exist, then return them parsed."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while True:
        lines = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if len(lines) >= n or loop.time() >= deadline:
            return lines
        await asyncio.sleep(0.01)


async def test_recorder_writes_published_envelopes_in_order(tmp_path: Path) -> None:
    bus = EventBus()
    out = tmp_path / "session.jsonl"
    task = asyncio.create_task(run_recorder(bus, out))
    try:
        await asyncio.sleep(0)  # let the subscribe register
        await bus.publish(type_="power", data=PowerData(watts=150), device_kind="mock")
        await bus.publish(type_="cadence", data=CadenceData(rpm=90), device_kind="mock")
        lines = await _drain_n_lines(out, 2)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert [line["type"] for line in lines] == ["power", "cadence"]
    assert [line["seq"] for line in lines] == [0, 1]
    assert lines[0]["data"] == {"watts": 150}


async def test_recorder_receives_session_state_replay(tmp_path: Path) -> None:
    """A recorder attached mid-session still captures session_start + device_connected
    so the resulting file is self-interpretable."""
    bus = EventBus()
    await bus.publish(
        type_="session_start",
        data=SessionStartData(session_id=bus.session_id),
        device_kind="mock",
    )
    await bus.publish(
        type_="device_connected",
        data=DeviceConnectedData(kind="bike_trainer", name="KICKR"),
        device_kind="bike_trainer",
    )

    out = tmp_path / "midride.jsonl"
    task = asyncio.create_task(run_recorder(bus, out))
    try:
        await asyncio.sleep(0)
        await bus.publish(type_="power", data=PowerData(watts=200), device_kind="bike_trainer")
        lines = await _drain_n_lines(out, 3)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    types = [line["type"] for line in lines]
    assert "session_start" in types
    assert "device_connected" in types
    assert "power" in types


async def test_recorder_creates_parent_directory(tmp_path: Path) -> None:
    bus = EventBus()
    out = tmp_path / "nested" / "subdir" / "session.jsonl"
    task = asyncio.create_task(run_recorder(bus, out))
    try:
        await asyncio.sleep(0)
        await bus.publish(type_="power", data=PowerData(watts=100), device_kind="mock")
        await _drain_n_lines(out, 1)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert out.exists()
