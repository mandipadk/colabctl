"""Adversarial tests for the browser-bridge JSON-RPC relay + session parsing."""

from __future__ import annotations

import asyncio
import json

import pytest

from colabctl.errors import TransportError
from colabctl.models import Accelerator, SessionStatus, Variant
from colabctl.transport.browser.bridge import BrowserBridgeTransport, _JsonRpcClient


class ControllablePeer:
    """A WebSocket double whose inbound frames the test pushes by hand."""

    _CLOSE = object()

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._q: asyncio.Queue = asyncio.Queue()

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def push_raw(self, raw: str) -> None:
        self._q.put_nowait(raw)

    def respond(self, msg_id: int, *, result=None, error=None) -> None:
        body = {"jsonrpc": "2.0", "id": msg_id}
        body["error" if error is not None else "result"] = error if error is not None else result
        self.push_raw(json.dumps(body))

    def close_stream(self) -> None:
        self._q.put_nowait(self._CLOSE)

    def fail_stream(self, exc: BaseException) -> None:
        self._q.put_nowait(exc)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self._q.get()
        if item is self._CLOSE:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item


async def _started(peer):
    client = _JsonRpcClient(peer)
    client.start()
    return client


async def test_out_of_order_responses_correlate():
    peer = ControllablePeer()
    client = await _started(peer)
    t1 = asyncio.create_task(client.call("m1", {}, timeout=5))
    t2 = asyncio.create_task(client.call("m2", {}, timeout=5))
    await asyncio.sleep(0.02)
    ids = [m["id"] for m in peer.sent]
    assert ids[0] != ids[1]  # distinct correlation ids
    peer.respond(ids[1], result="r2")  # respond to the SECOND call first
    peer.respond(ids[0], result="r1")
    assert await t1 == "r1"
    assert await t2 == "r2"
    await client.close()


async def test_malformed_frame_is_skipped():
    peer = ControllablePeer()
    client = await _started(peer)
    t = asyncio.create_task(client.call("m", {}, timeout=5))
    await asyncio.sleep(0.02)
    peer.push_raw("not json {{{")  # must not kill the reader
    peer.respond(peer.sent[0]["id"], result="ok")
    assert await t == "ok"
    await client.close()


async def test_unknown_id_is_ignored():
    peer = ControllablePeer()
    client = await _started(peer)
    t = asyncio.create_task(client.call("m", {}, timeout=5))
    await asyncio.sleep(0.02)
    peer.respond(999999, result="ghost")  # no such pending call
    peer.respond(peer.sent[0]["id"], result="real")
    assert await t == "real"
    await client.close()


async def test_error_response_raises_transport_error():
    peer = ControllablePeer()
    client = await _started(peer)
    t = asyncio.create_task(client.call("m", {}, timeout=5))
    await asyncio.sleep(0.02)
    peer.respond(peer.sent[0]["id"], error={"code": -1, "message": "boom"})
    with pytest.raises(TransportError):
        await t
    await client.close()


async def test_clean_close_fails_pending_fast():
    peer = ControllablePeer()
    client = await _started(peer)
    t = asyncio.create_task(client.call("m", {}, timeout=30))  # long timeout
    await asyncio.sleep(0.02)
    peer.close_stream()
    # must fail well before the 30s call timeout thanks to the clean-close handling
    with pytest.raises(TransportError):
        await asyncio.wait_for(t, timeout=2)
    await client.close()


async def test_stream_error_fails_pending():
    peer = ControllablePeer()
    client = await _started(peer)
    t = asyncio.create_task(client.call("m", {}, timeout=30))
    await asyncio.sleep(0.02)
    peer.fail_stream(RuntimeError("socket died"))
    with pytest.raises(TransportError):
        await asyncio.wait_for(t, timeout=2)
    await client.close()


async def test_call_timeout_cleans_pending():
    peer = ControllablePeer()
    client = await _started(peer)
    with pytest.raises(TransportError):
        await client.call("m", {}, timeout=0.05)  # never answered
    assert client._pending == {}  # the timed-out future was popped
    await client.close()


# --- _session_info defaulting (malformed frontend data) ---------------------


def test_session_info_defaults_on_bad_enum_values():
    info = BrowserBridgeTransport._session_info(
        {"name": "s", "accelerator": "RTX9090", "variant": "FOO", "status": "WAT"}
    )
    assert info.accelerator is Accelerator.NONE
    assert info.variant is Variant.DEFAULT  # was a latent ValueError before the fix
    assert info.status is SessionStatus.UNKNOWN


def test_session_info_parses_valid_values():
    info = BrowserBridgeTransport._session_info(
        {"name": "s", "endpoint": "ep", "accelerator": "A100", "variant": "GPU", "status": "BUSY"}
    )
    assert info.accelerator is Accelerator.A100
    assert info.variant is Variant.GPU
    assert info.status is SessionStatus.BUSY


def test_session_info_handles_missing_fields():
    info = BrowserBridgeTransport._session_info({})
    assert info.name == "" and info.endpoint == ""
    assert info.accelerator is Accelerator.NONE
    assert info.variant is Variant.DEFAULT
    assert info.status is SessionStatus.UNKNOWN
