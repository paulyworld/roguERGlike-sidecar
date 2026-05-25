"""Send a single JSON command to a running sidecar's WS server.

Invoked by ``send-cmd.ps1``. Reads the command from stdin (not argv)
because Windows PowerShell's native-command argument passing strips
embedded double quotes when invoking external programs — so passing
JSON as an argv arrives mangled. Stdin is a binary pipe; quotes
survive.

Usage:
    echo '{"type":"resume"}' | python scripts/_send_cmd.py <ws-url>
"""

from __future__ import annotations

import asyncio
import json
import sys

import websockets


async def _send(url: str, cmd: str) -> None:
    async with websockets.connect(url) as ws:
        await ws.send(cmd)
        print("sent -> " + url + ": " + cmd)


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: <cmd-on-stdin> | python scripts/_send_cmd.py <ws-url>", file=sys.stderr)
        sys.exit(2)
    cmd = sys.stdin.read().strip()
    # Validate as JSON before sending so a typo is caught client-side
    # rather than at the sidecar's parse + drop path.
    json.loads(cmd)
    asyncio.run(_send(sys.argv[1], cmd))


if __name__ == "__main__":
    main()
