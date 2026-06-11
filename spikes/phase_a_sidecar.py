#!/usr/bin/env python3
"""Phase A — Colab "local MCP server" protocol capture (Track A + P14).

Live captures (2026-06-11) established the channel: Colab's "Connect to a local Colab MCP
server" connects to a local WebSocket (subprotocol ``mcp``, token via ``?access_token=``)
and then sends ``notifications/tools/list_changed`` — an MCP *server→client* notification.
So on this link **Colab is the MCP server exposing its own tools**, and we are the client.

This drives the client side: on connect it sends ``initialize`` → ``notifications/
initialized`` → ``tools/list`` and prints what Colab exposes. If Colab offers a tool that
runs in / keeps the authenticated runtime alive, Track A's keep-alive sidecar is viable;
if not, Track A pivots to Track B (cookie/SAPISIDHASH). It also answers any requests Colab
sends back, in case the link is bidirectional. Pure capture — nothing here is product code.

Run:  uv run --extra browser python spikes/phase_a_sidecar.py
      (open the tab it prints — append ?authuser=N for the right account — and Connect)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import sys
import webbrowser
from typing import Any
from urllib.parse import parse_qs, urlsplit

_COLAB_HOST = "https://colab.research.google.com"
_MCP_PROTOCOL_VERSION = "2024-11-05"


def _mcp_reply(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Minimal valid response if Colab *also* drives us as a server (bidirectional link)."""
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": msg.get("params", {}).get(
                    "protocolVersion", _MCP_PROTOCOL_VERSION
                ),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "colabctl-sidecar-probe", "version": "0.1"},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": []}}
    if method in ("resources/list", "prompts/list"):
        return {"jsonrpc": "2.0", "id": mid, "result": {method.split("/", 1)[0]: []}}
    if mid is not None and method:
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    return None


async def main() -> None:
    try:
        import websockets
    except ImportError:
        print("install the browser extra: uv sync --extra browser", file=sys.stderr)
        sys.exit(2)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("websockets").setLevel(logging.INFO)  # DEBUG for raw handshake frames

    token = secrets.token_urlsafe(24)

    async def handler(ws: Any) -> None:
        req = getattr(ws, "request", None)
        path = getattr(req, "path", "") if req is not None else ""
        origin = req.headers.get("Origin", "") if req is not None else ""
        access_token = (parse_qs(urlsplit(path).query).get("access_token") or [""])[0]
        print(
            f"[conn] origin={origin!r} subprotocol={ws.subprotocol!r} "
            f"token_ok={access_token == token}",
            flush=True,
        )
        if (origin and not origin.startswith(_COLAB_HOST)) or access_token != token:
            await ws.close()
            return

        async def send(obj: dict[str, Any]) -> None:
            text = json.dumps(obj)
            await ws.send(text)
            print(f"  >>> {text}", flush=True)

        # Drive the MCP client handshake; Colab (the server) answers with its tools.
        await send(
            {
                "jsonrpc": "2.0",
                "id": "init",
                "method": "initialize",
                "params": {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "colabctl-sidecar", "version": "0.1"},
                },
            }
        )
        initialized = False
        async for raw in ws:
            print(f"  <<< {raw}", flush=True)
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            mid, method = msg.get("id"), msg.get("method")
            if mid == "init" and "result" in msg:
                initialized = True
                await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                await send({"jsonrpc": "2.0", "id": "tools", "method": "tools/list", "params": {}})
            elif mid == "tools" and "result" in msg:
                print(
                    "\n  *** Colab MCP tools ***\n"
                    + json.dumps(msg.get("result"), indent=2)
                    + "\n",
                    flush=True,
                )
            elif method == "notifications/tools/list_changed" and initialized:
                await send({"jsonrpc": "2.0", "id": "tools", "method": "tools/list", "params": {}})
            elif method and mid is not None:  # Colab is also asking us something
                reply = _mcp_reply(msg)
                if reply is not None:
                    await send(reply)

    server = await websockets.serve(handler, "127.0.0.1", 0, subprotocols=["mcp"])
    port = server.sockets[0].getsockname()[1]
    url = f"{_COLAB_HOST}/notebooks/empty.ipynb#mcpProxyToken={token}&mcpProxyPort={port}"
    print(f"[sidecar] MCP server on ws://127.0.0.1:{port} (subprotocol 'mcp')", flush=True)
    print("[sidecar] open this tab and Connect (add ?authuser=N if needed):", flush=True)
    print(f"  {url}\n", flush=True)
    webbrowser.open(url)
    try:
        await asyncio.Future()  # serve until Ctrl-C; the handler drives + logs MCP
    finally:
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
