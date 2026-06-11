"""Integration tests for the native client's HTTP handling (XSSI, 4xx, 5xx retry)."""

from __future__ import annotations

import uuid

import httpx
import pytest

from colabctl.errors import (
    AcceleratorUnavailableError,
    KernelError,
    RuntimeUnavailableError,
    TooManyAssignmentsError,
)
from colabctl.models import Accelerator
from colabctl.transport.native.client import PROXY_TOKEN_HEADER, ColabBackendClient

_XSSI = ")]}'\n"


async def test_request_strips_xssi_and_parses_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_XSSI + '{"assignments": []}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ColabBackendClient(http)
        assert await client.list_assignments() == []


async def test_412_raises_too_many_assignments():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="too many")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ColabBackendClient(http)
        with pytest.raises(TooManyAssignmentsError):
            await client.assign(accelerator=Accelerator.T4)


async def test_5xx_is_retried_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="server busy")
        return httpx.Response(200, text=_XSSI + '{"assignments": []}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ColabBackendClient(http)
        assert await client.list_assignments() == []
        assert calls["n"] == 2  # retried past the 503


async def test_assign_400_raises_accelerator_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=_XSSI + '{"token": "xsrf"}')
        return httpx.Response(400, text="not entitled to A100")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ColabBackendClient(http)
        with pytest.raises(AcceleratorUnavailableError) as ei:
            await client.assign(accelerator=Accelerator.A100)
        assert ei.value.accelerator == "A100"


async def test_ccu_info_returns_parsed_dict():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_XSSI + '{"computeUnits": 42.5}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ColabBackendClient(http)
        assert await client.ccu_info() == {"computeUnits": 42.5}


async def test_refresh_assignment_returns_existing_runtime_get_only():
    methods: list[str] = []
    body = (
        '{"endpoint": "gpu-x", "accelerator": "T4", "variant": 1, '
        '"runtimeProxyInfo": {"token": "ptok2", "tokenExpiresInSeconds": 600, '
        '"url": "https://x/tun/m/gpu-x"}}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, text=_XSSI + body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ColabBackendClient(http)
        assignment = await client.refresh_assignment(uuid.uuid4(), accelerator=Accelerator.T4)
    assert assignment.endpoint == "gpu-x"
    assert assignment.runtime_proxy_info is not None
    assert assignment.runtime_proxy_info.token == "ptok2"
    assert methods == ["GET"]  # GET-only: never POSTs, so it can't allocate a new runtime


async def test_refresh_assignment_raises_when_reclaimed_without_posting():
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        # Only an XSRF token (the prelude to a NEW allocation) — runtime is gone.
        return httpx.Response(200, text=_XSSI + '{"token": "xsrf"}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ColabBackendClient(http)
        with pytest.raises(RuntimeUnavailableError):
            await client.refresh_assignment(uuid.uuid4(), accelerator=Accelerator.T4)
    assert methods == ["GET"]  # refused to allocate a replacement


async def test_interrupt_kernel_uses_proxy_token_header_and_route():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["method"] = request.method
        seen["token"] = request.headers.get(PROXY_TOKEN_HEADER, "")
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ColabBackendClient(http)
        await client.interrupt_kernel("https://proxy.example/tun/m/ep", "kid", proxy_token="ptok")
    assert seen["method"] == "POST"
    assert seen["path"].endswith("/api/kernels/kid/interrupt")
    assert seen["token"] == "ptok"  # header-only proxy auth, no bearer


async def test_interrupt_kernel_non_2xx_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="kernel busy")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ColabBackendClient(http)
        with pytest.raises(KernelError):
            await client.interrupt_kernel("https://proxy.example", "kid", proxy_token="ptok")
