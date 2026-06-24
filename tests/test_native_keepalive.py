"""Tunnel keep-alive (Phase 0.6): the google-colab-cli recipe + adapter wiring.

The client method is asserted against the exact HTTP recipe; the adapter is asserted to
prefer the tunnel ping (no kernel needed) and fall back to kernel activity when it fails.
``Capabilities.keepalive`` stays False until a live run confirms the lease actually holds
past idle (see spikes/phase_b_keepalive.py) — this only proves the request is well-formed.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from colabctl.errors import KeepAliveError
from colabctl.models import Accelerator, RuntimeSpec
from colabctl.state import StateStore
from colabctl.transport.native.client import (
    TUNNEL_HEADER,
    TUNNEL_HEADER_VALUE,
    ColabBackendClient,
)
from test_native_attach import FakeClient, _mk


async def _token() -> str:
    return "tok-123"


async def test_tunnel_keep_alive_sends_the_recipe():
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["tunnel"] = request.headers.get(TUNNEL_HEADER)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, text="ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ColabBackendClient(http, token_provider=_token)
        await client.tunnel_keep_alive("ep-abc")

    assert "/tun/m/ep-abc/keep-alive/" in seen["url"]
    assert "authuser=0" in seen["url"]  # required on the frontend host, else HTTP 400
    assert seen["tunnel"] == TUNNEL_HEADER_VALUE  # X-Colab-Tunnel: Google
    assert seen["auth"] == "Bearer tok-123"  # rides the ordinary bearer token


async def test_tunnel_keep_alive_treats_read_timeout_as_success():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("held open", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ColabBackendClient(http, token_provider=_token)
        await client.tunnel_keep_alive("ep")  # must NOT raise — the tunnel held the request


async def test_tunnel_keep_alive_raises_on_real_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="denied")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ColabBackendClient(http, token_provider=_token)
        with pytest.raises(KeepAliveError, match="tunnel keep-alive failed"):
            await client.tunnel_keep_alive("ep")


@pytest.fixture
def state(tmp_path: Path) -> StateStore:
    return StateStore(home=tmp_path / "home")


async def test_adapter_keep_alive_prefers_tunnel_ping(state: StateStore) -> None:
    client = FakeClient()
    transport, kernels = _mk(client, state)
    info = await transport.allocate(RuntimeSpec(accelerator=Accelerator.T4, name="ka"))

    await transport.keep_alive(info.name)

    # Used the tunnel ping for the session's endpoint; did not spin up a kernel for it.
    record = state.get_session(info.name)
    assert record is not None
    assert client.tunnel_pings == [record.endpoint]
    assert all("None" not in k.codes for k in kernels)


async def test_adapter_keep_alive_falls_back_to_kernel(state: StateStore) -> None:
    client = FakeClient()
    client.tunnel_fails = True
    transport, kernels = _mk(client, state)
    info = await transport.allocate(RuntimeSpec(accelerator=Accelerator.T4, name="ka"))

    await transport.keep_alive(info.name)

    # Tunnel ping was attempted, then it fell back to a kernel-activity ping.
    assert len(client.tunnel_pings) == 1
    assert any("None" in k.codes for k in kernels)
