#!/usr/bin/env python3
"""Phase A — browser sidecar/bridge protocol capture (Track A + P14).

The BrowserBridgeTransport currently *guesses* the colab-mcp JSON-RPC method/result
shapes (allocateRuntime/execute/...); they are unconfirmed against the live frontend.
This spike stands up the same origin-checked, token-handshook local WebSocket relay the
bridge uses, opens a Colab tab pointed back at it, and **logs every frame the page
sends** — so we can read the real protocol instead of guessing, and wire the keep-alive
sidecar (Track A) to whatever the frontend actually speaks.

It does not assume a protocol: it records the hello + any announce/notification frames,
and lets you type a raw JSON-RPC request line on stdin to probe a method and see the
reply. Nothing here is load-bearing product code — it's a listening post.

Run:  uv run --extra browser python spikes/phase_a_sidecar.py
      (then complete login in the opened tab; watch the frames; Ctrl-C to stop)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import sys
import webbrowser
from typing import Any

_COLAB_HOST = "https://colab.research.google.com"


async def _pump_stdin(ws: Any) -> None:
    """Forward raw JSON lines typed on stdin to the page (for probing methods)."""
    loop = asyncio.get_running_loop()
    print("  (type a JSON-RPC request line to send, e.g. "
          '{"jsonrpc":"2.0","id":1,"method":"listRuntimes","params":{}})', flush=True)
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        try:
            json.loads(line)  # validate before sending
        except ValueError:
            print(f"  not valid JSON, ignored: {line!r}", flush=True)
            continue
        await ws.send(line)
        print(f"  >>> sent: {line}", flush=True)


async def main() -> None:
    try:
        import websockets
    except ImportError:
        print("install the browser extra: uv sync --extra browser", file=sys.stderr)
        sys.exit(2)

    # Surface the WebSocket *handshake* — if Colab's MCP client connects but never reaches
    # our handler (no `[conn]` line), the cause is here: a rejected upgrade, a required
    # subprotocol we don't echo, or the connection never arriving (page CSP/mixed-content).
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("websockets").setLevel(logging.DEBUG)

    token = secrets.token_urlsafe(24)
    connected: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    async def handler(ws: Any) -> None:
        req = getattr(ws, "request", None)
        origin = req.headers.get("Origin", "") if req is not None else ""
        path = getattr(req, "path", "?") if req is not None else "?"
        subprotos = ws.request.headers.get("Sec-WebSocket-Protocol", "") if req is not None else ""
        print(f"[conn] path={path!r} origin={origin!r} subprotocols={subprotos!r}", flush=True)
        if origin and not origin.startswith(_COLAB_HOST):
            print("  rejecting non-Colab origin", flush=True)
            await ws.close()
            return
        if not connected.done():
            connected.set_result(ws)
        async for raw in ws:
            print(f"  <<< {raw}", flush=True)

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    url = f"{_COLAB_HOST}/notebooks/empty.ipynb#mcpProxyToken={token}&mcpProxyPort={port}"
    print(f"[sidecar] relay on ws://127.0.0.1:{port}", flush=True)
    print(f"[sidecar] open this tab and finish login:\n  {url}\n", flush=True)
    webbrowser.open(url)

    try:
        ws = await asyncio.wait_for(connected, timeout=300)
        print("[sidecar] page connected — capturing frames (Ctrl-C to stop)", flush=True)
        await _pump_stdin(ws)
    except TimeoutError:
        print("[sidecar] no connection within 5 min; is colab-mcp enabled in the tab?", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
