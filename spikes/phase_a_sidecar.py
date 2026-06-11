#!/usr/bin/env python3
"""Phase A — Colab "local MCP server" protocol capture (Track A + P14).

Live capture (2026-06-11) showed Colab's "Connect to a local Colab MCP server" feature
connects to a local WebSocket **as an MCP client**: it requests the ``mcp`` subprotocol,
authenticates via an ``?access_token=<token>`` query param, then speaks the Model Context
Protocol (JSON-RPC 2.0). A server that doesn't *negotiate the ``mcp`` subprotocol* is
dropped instantly ("disconnected from local mcp server").

This stands up exactly that: a minimal MCP server over WS (subprotocol ``mcp``, token via
query param) that logs every frame and answers ``initialize``/``tools/list`` with valid
stubs so the session proceeds — revealing the real handshake and what each side declares
(which decides whether Track A's keep-alive idea is even viable through this channel).

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
    """A minimal valid MCP response so the client keeps the session open (capture aid)."""
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
        key = method.split("/", 1)[0]
        return {"jsonrpc": "2.0", "id": mid, "result": {key: []}}
    if mid is not None and method:  # ack any other request so the client proceeds
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    return None  # notifications (no id) need no reply


async def main() -> None:
    try:
        import websockets
    except ImportError:
        print("install the browser extra: uv sync --extra browser", file=sys.stderr)
        sys.exit(2)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("websockets").setLevel(logging.INFO)  # DEBUG for full handshake frames

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
        if origin and not origin.startswith(_COLAB_HOST):
            await ws.close()
            return
        if access_token != token:
            print("  token mismatch — closing", flush=True)
            await ws.close()
            return
        async for raw in ws:
            print(f"  <<< {raw}", flush=True)
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            reply = _mcp_reply(msg)
            if reply is not None:
                out = json.dumps(reply)
                await ws.send(out)
                print(f"  >>> {out}", flush=True)

    # subprotocols=["mcp"] is the fix: the client requires it negotiated or it disconnects.
    server = await websockets.serve(handler, "127.0.0.1", 0, subprotocols=["mcp"])
    port = server.sockets[0].getsockname()[1]
    url = f"{_COLAB_HOST}/notebooks/empty.ipynb#mcpProxyToken={token}&mcpProxyPort={port}"
    print(f"[sidecar] MCP server on ws://127.0.0.1:{port} (subprotocol 'mcp')", flush=True)
    print("[sidecar] open this tab and Connect (add ?authuser=N if needed):", flush=True)
    print(f"  {url}\n", flush=True)
    webbrowser.open(url)
    try:
        await asyncio.Future()  # serve until Ctrl-C; the handler logs frames
    finally:
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
