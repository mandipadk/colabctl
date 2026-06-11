"""Minimal MCP (Model Context Protocol) client over a connected WebSocket.

Colab's "local Colab MCP server" connects to us and acts as the MCP *server* — it exposes
notebook tools (``add_code_cell``, ``run_code_cell``, ``get_cells``, …) and announces them
with ``notifications/tools/list_changed``. We are the *client*: this drives the standard
handshake (``initialize`` → ``notifications/initialized``) and exposes ``list_tools`` /
``call_tool`` with JSON-RPC id correlation. The websocket is injected, so the whole thing
is testable against an in-memory ColabMCP fake (no browser).

Protocol confirmed live in Phase A (2026-06-11): subprotocol ``mcp``, token via the
``?access_token=`` query param, ``serverInfo.name == "ColabMCP"``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from colabctl.errors import TransportError

_PROTOCOL_VERSION = "2024-11-05"


def mcp_text(result: dict[str, Any]) -> str:
    """Concatenate the ``text`` parts of an MCP ``tools/call`` result's content."""
    return "".join(
        part.get("text", "")
        for part in (result.get("content") or [])
        if isinstance(part, dict) and part.get("type") == "text"
    )


class McpClient:
    """An MCP client over a connected WebSocket (the peer is the MCP server)."""

    def __init__(self, ws: Any, *, default_timeout: float = 120.0) -> None:
        self._ws = ws
        self._timeout = default_timeout
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader: asyncio.Task[None] | None = None
        self.server_info: dict[str, Any] | None = None

    def start(self) -> None:
        """Begin reading frames (must run before any request so replies aren't missed)."""
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                mid = msg.get("id")
                if mid is None:  # a notification (e.g. tools/list_changed) — no reply expected
                    continue
                fut = self._pending.pop(mid, None)
                if fut is None or fut.done():
                    continue
                if "error" in msg:
                    fut.set_exception(TransportError(f"MCP error: {msg['error']}"))
                else:
                    fut.set_result(msg.get("result"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_all(TransportError(f"MCP connection lost: {exc}"))
        else:
            self._fail_all(TransportError("MCP connection closed by peer"))

    def _fail_all(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        self._next_id += 1
        mid = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send(
            json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        )
        try:
            return await asyncio.wait_for(fut, timeout or self._timeout)
        except TimeoutError as exc:
            self._pending.pop(mid, None)
            raise TransportError(f"MCP request {method!r} timed out") from exc

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._ws.send(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}})
        )

    async def initialize(self) -> dict[str, Any]:
        result = await self._request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "colabctl", "version": "0.1"},
            },
        )
        self.server_info = (result or {}).get("serverInfo")
        await self._notify("notifications/initialized")
        return result or {}

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._request("tools/list")
        tools: list[dict[str, Any]] = (result or {}).get("tools", [])
        return tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        result = await self._request(
            "tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout
        )
        return result or {}

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None


__all__ = ["McpClient", "mcp_text"]
