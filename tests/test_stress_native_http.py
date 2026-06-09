"""Adversarial tests for native-client error mapping + malformed-response handling."""

from __future__ import annotations

import httpx
import pytest

from colabctl.errors import AllocationError, TransportError
from colabctl.models import Accelerator
from colabctl.transport.native.client import ColabBackendClient, assignment_from_wire

_XSSI = ")]}'\n"


def _client(handler) -> tuple[ColabBackendClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ColabBackendClient(http), http


# --- malformed responses map to typed errors (not KeyError/JSONDecodeError) -


async def test_non_json_body_raises_transport_error():
    def handler(request):
        return httpx.Response(200, text=_XSSI + "this is not json")

    client, http = _client(handler)
    async with http:
        with pytest.raises(TransportError):
            await client.list_assignments()


async def test_assign_post_missing_endpoint_raises_allocation_error():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, text=_XSSI + '{"token": "xsrf"}')
        # POST 200 but the object has no 'endpoint' — contract drift
        return httpx.Response(200, text=_XSSI + '{"runtimeProxyInfo": {}}')

    client, http = _client(handler)
    async with http:
        with pytest.raises(AllocationError):
            await client.assign(accelerator=Accelerator.T4)


async def test_assign_get_non_object_raises_allocation_error():
    def handler(request):
        return httpx.Response(200, text=_XSSI + "[1, 2, 3]")  # a list, not an object

    client, http = _client(handler)
    async with http:
        with pytest.raises(AllocationError):
            await client.assign(accelerator=Accelerator.T4)


async def test_assign_get_without_token_raises_allocation_error():
    def handler(request):
        return httpx.Response(200, text=_XSSI + "{}")  # no endpoint, no token

    client, http = _client(handler)
    async with http:
        with pytest.raises(AllocationError):
            await client.assign(accelerator=Accelerator.T4)


async def test_list_assignments_with_malformed_entry_raises():
    def handler(request):
        return httpx.Response(200, text=_XSSI + '{"assignments": [{"accelerator": "T4"}]}')

    client, http = _client(handler)
    async with http:
        with pytest.raises(AllocationError):
            await client.list_assignments()


def test_assignment_from_wire_missing_endpoint_unit():
    with pytest.raises(AllocationError):
        assignment_from_wire({"accelerator": "T4"})


# --- happy-path shortcuts + request shaping ---------------------------------


async def test_assign_returns_existing_assignment_without_post():
    seen = {"methods": []}

    def handler(request):
        seen["methods"].append(request.method)
        return httpx.Response(
            200,
            text=_XSSI
            + '{"endpoint": "ep1", "runtimeProxyInfo": '
            + '{"token": "t", "tokenExpiresInSeconds": 3600, "url": "https://u"}}',
        )

    client, http = _client(handler)
    async with http:
        a = await client.assign(accelerator=Accelerator.T4)
    assert a.endpoint == "ep1"
    assert a.runtime_proxy_info.token == "t"
    assert seen["methods"] == ["GET"]  # short-circuited; no POST issued


async def test_authuser_added_on_colab_domain():
    captured = {}

    def handler(request):
        captured["authuser"] = request.url.params.get("authuser")
        return httpx.Response(200, text=_XSSI + '{"assignments": []}')

    client, http = _client(handler)
    async with http:
        await client.list_assignments()
    assert captured["authuser"] == "0"


async def test_bearer_token_is_applied():
    captured = {}

    async def provider():
        return "tok-123"

    def handler(request):
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, text=_XSSI + '{"assignments": []}')

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ColabBackendClient(http, token_provider=provider)
    async with http:
        await client.list_assignments()
    assert captured["auth"] == "Bearer tok-123"
