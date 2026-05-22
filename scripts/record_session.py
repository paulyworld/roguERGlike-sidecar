"""Record every WS envelope emitted by a running sidecar to JSONL.

Pairs naturally with the live test: start the sidecar, then this recorder
in a second window. Every event the sidecar publishes lands as one JSON
line per file row. Includes the session-state replay the bus does on
subscribe, so the recording is self-contained even if you started it
mid-session.

Usage:
    python scripts/record_session.py                # default output path
    python scripts/record_session.py -o my_test.jsonl
    python scripts/record_session.py --url ws://other-host:8421

Stop with Ctrl+C. The file is flushed on every write so a crash mid-ride
still leaves a usable recording.

Output goes under ``docs/recordings/`` by default. **The recording WILL
contain heart-rate samples and other personal telemetry** — keep that
directory out of git (see ``.gitignore``).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import time
from pathlib import Path

import websockets

DEFAULT_URL = "ws://localhost:8421"


async def _record(url: str, out_path: Path) -> None:
    print(f"Recording → {out_path}")
    print(f"Connecting to {url} ...")
    n = 0
    started_at = time.time()
    with out_path.open("w", encoding="utf-8") as f:
        async with websockets.connect(url) as ws:
            print("Connected. Recording until Ctrl+C.\n")
            async for msg in ws:
                f.write(msg + "\n")
                f.flush()
                n += 1
                if n % 50 == 0:
                    elapsed = time.time() - started_at
                    print(f"  {n:>5} events recorded ({elapsed:.0f}s elapsed)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output JSONL path. Default: docs/recordings/YYYYMMDD_HHMMSS.jsonl",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"Sidecar WebSocket URL (default: {DEFAULT_URL}).",
    )
    args = parser.parse_args()

    if args.output is None:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        out_dir = Path(__file__).resolve().parent.parent / "docs" / "recordings"
        out_dir.mkdir(parents=True, exist_ok=True)
        args.output = out_dir / f"{stamp}.jsonl"
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_record(args.url, args.output))
    print(f"\nstopped. {args.output} kept.")


if __name__ == "__main__":
    main()
