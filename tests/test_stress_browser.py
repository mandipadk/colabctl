"""Adversarial tests for the MCP client (error responses, close, timeout, notifications)."""

from __future__ import annotations

import asyncio
import json

import pytest

from colabctl.errors import TransportError
from colabctl.transport.browser.mcp import McpClient


class ControllablePeer:
    """A ws double whose inbound frames the test pushes by hand."""

    _CLOSE = object()

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._q: asyncio.Queue = asyncio.Queue()

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def __aiter__(self) -> ControllablePeer:
        return self

    async def __anext__(self) -> str:
        item = await self._q.get()
        if item is self._CLOSE:
            raise StopAsyncIteration
        return item

    async def push(self, msg: dict) -> None:
        await self._q.put(json.dumps(msg))

    async def close_peer(self) -> None:
        await self._q.put(self._CLOSE)


async def _await_first_send(peer: ControllablePeer) -> None:
    for _ in range(100):
        if peer.sent:
            return
        await asyncio.sleep(0)
    raise AssertionError("client never sent a request")


async def test_error_response_raises_transport_error():
    peer = ControllablePeer()
    client = McpClient(peer)
    client.start()
    task = asyncio.create_task(client.list_tools())
    await _await_first_send(peer)
    await peer.push({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "nope"}})
    with pytest.raises(TransportError, match="MCP error"):
        await task
    await client.close()


async def test_connection_closed_fails_pending_requests():
    peer = ControllablePeer()
    client = McpClient(peer)
    client.start()
    task = asyncio.create_task(client.call_tool("run_code_cell", {"cellId": "x"}))
    await _await_first_send(peer)
    await peer.close_peer()
    with pytest.raises(TransportError, match="closed"):
        await task
    await client.close()


async def test_request_times_out():
    peer = ControllablePeer()
    client = McpClient(peer, default_timeout=0.05)
    client.start()
    with pytest.raises(TransportError, match="timed out"):
        await client.list_tools()  # no response ever arrives
    await client.close()


async def test_server_notifications_are_ignored():
    peer = ControllablePeer()
    client = McpClient(peer)
    client.start()
    await peer.push({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})  # no id
    await asyncio.sleep(0.01)  # reader processes + ignores it
    task = asyncio.create_task(client.list_tools())
    await _await_first_send(peer)
    await peer.push({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
    assert await task == []  # still healthy after the stray notification
    await client.close()


async def test_unparseable_frame_is_skipped():
    peer = ControllablePeer()
    client = McpClient(peer)
    client.start()
    await peer._q.put("{ not json")  # malformed inbound frame
    await asyncio.sleep(0.01)
    task = asyncio.create_task(client.list_tools())
    await _await_first_send(peer)
    await peer.push({"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "run_code_cell"}]}})
    assert (await task)[0]["name"] == "run_code_cell"
    await client.close()
