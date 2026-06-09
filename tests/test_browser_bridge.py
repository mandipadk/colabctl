"""Tests for the browser-bridge JSON-RPC relay + transport mapping (no real browser)."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from colabctl.errors import RuntimeUnavailableError, TransportError
from colabctl.models import Accelerator, RuntimeSpec, SessionStatus
from colabctl.transport.browser.bridge import BrowserBridgeTransport, _JsonRpcClient


class FakeFrontendWS:
    """A loopback 'Colab frontend': answers each request via a responder."""

    def __init__(self, responder):
        self._responder = responder
        self._out: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, data: str) -> None:
        req = json.loads(data)
        try:
            result = self._responder(req["method"], req.get("params"))
            msg = {"jsonrpc": "2.0", "id": req["id"], "result": result}
        except Exception as exc:
            msg = {"jsonrpc": "2.0", "id": req["id"], "error": str(exc)}
        await self._out.put(json.dumps(msg))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        return await self._out.get()


async def test_jsonrpc_correlates_responses():
    client = _JsonRpcClient(FakeFrontendWS(lambda method, params: {"echo": method}))
    client.start()
    assert await client.call("allocateRuntime", {}) == {"echo": "allocateRuntime"}
    assert await client.call("execute", {}) == {"echo": "execute"}
    await client.close()


async def test_jsonrpc_error_becomes_transport_error():
    def responder(method, params):
        raise ValueError("frontend boom")

    client = _JsonRpcClient(FakeFrontendWS(responder))
    client.start()
    with pytest.raises(TransportError):
        await client.call("execute", {})
    await client.close()


class FakeRpc:
    def __init__(self, responses):
        self._responses = responses
        self.calls: list[tuple[str, dict | None]] = []

    async def call(self, method, params=None, *, timeout=120.0):
        self.calls.append((method, params))
        value = self._responses.get(method)
        return value(params) if callable(value) else value

    async def close(self):
        pass


def _bridge(responses):
    return BrowserBridgeTransport(_rpc=FakeRpc(responses), open_browser=False)


async def test_allocate_maps_session_info():
    bridge = _bridge(
        {
            "allocateRuntime": {
                "name": "j",
                "endpoint": "ep-j",
                "accelerator": "T4",
                "variant": "GPU",
                "status": "IDLE",
            }
        }
    )
    info = await bridge.allocate(RuntimeSpec(accelerator=Accelerator.T4, name="j"))
    assert info.name == "j"
    assert info.accelerator is Accelerator.T4
    assert info.status is SessionStatus.IDLE


async def test_execute_maps_outputs():
    bridge = _bridge(
        {
            "execute": {
                "status": "ok",
                "outputs": [{"output_type": "stream", "name": "stdout", "text": "hi"}],
            }
        }
    )
    result = await bridge.execute("j", "print(1)")
    assert result.ok
    assert result.stdout == "hi"


async def test_upload_and_download(tmp_path):
    captured = {}

    def upload(params):
        captured["upload"] = params
        return {}

    def download(params):
        return {"dataB64": base64.b64encode(b"remote-data").decode()}

    bridge = _bridge({"uploadFile": upload, "downloadFile": download})
    local = tmp_path / "a.txt"
    local.write_bytes(b"remote-data")
    await bridge.upload("j", local, "content/a.txt")
    assert captured["upload"]["remote"] == "content/a.txt"
    assert base64.b64decode(captured["upload"]["dataB64"]) == b"remote-data"

    dest = tmp_path / "b.txt"
    await bridge.download("j", "content/a.txt", dest)
    assert dest.read_bytes() == b"remote-data"


async def test_operations_require_start():
    bridge = BrowserBridgeTransport(open_browser=False)  # no rpc injected, not started
    with pytest.raises(RuntimeUnavailableError):
        await bridge.allocate(RuntimeSpec(name="j"))


def test_capabilities_not_headless_and_discloses_status():
    caps = BrowserBridgeTransport(open_browser=False).capabilities
    assert caps.headless is False  # needs a browser tab
    assert caps.interactive is True
    assert any("not live-validated" in c.lower() for c in caps.caveats)
