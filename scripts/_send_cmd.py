"""Send a single JSON command to a running sidecar's WS server.

Invoked by ``send-cmd.ps1``. Kept as a separate file rather than an
inline ``python -c`` block in the PS wrapper because Windows
PowerShell's native-command quoting mangles multi-line ``-c`` sources
(the embedded ``"`` quotes get stripped and the source word-splits at
spaces, producing ``SyntaxError: '(' was never closed`` style errors).

Usage:
    python scripts/_send_cmd.py <ws-url> <json-command>
"""

from __future__ import annotations

import asyncio
import sys

import websockets


async def _send(url: str, cmd: str) -> None:
    async with websockets.connect(url) as ws:
        await ws.send(cmd)
        print("sent -> " + url + ": " + cmd)


def main() -> None:
    if len(sys.argv) != 3:
        print("usage: python scripts/_send_cmd.py <ws-url> <json-command>", file=sys.stderr)
        sys.exit(2)
    asyncio.run(_send(sys.argv[1], sys.argv[2]))


if __name__ == "__main__":
    main()
