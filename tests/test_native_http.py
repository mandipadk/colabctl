"""Integration tests for the native client's HTTP handling (XSSI, 4xx, 5xx retry)."""

from __future__ import annotations

import httpx
import pytest

from colabctl.errors import AcceleratorUnavailableError, TooManyAssignmentsError
from colabctl.models import Accelerator
from colabctl.transport.native.client import ColabBackendClient

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
