"""Auth diagnostics: tokeninfo introspection + quota-project exposure."""

from __future__ import annotations

import httpx
import pytest

from colabctl.auth import ADCAuthProvider, StaticTokenProvider
from colabctl.auth.diagnostics import COLABORATORY_SCOPE, scopes_of, token_info
from colabctl.errors import AuthError


async def test_token_info_returns_json_and_scopes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("access_token") == "tok"
        return httpx.Response(
            200, json={"email": "a@b.com", "scope": f"openid {COLABORATORY_SCOPE}"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        info = await token_info("tok", http=http)
    assert info["email"] == "a@b.com"
    assert COLABORATORY_SCOPE in scopes_of(info)


async def test_token_info_non_200_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="invalid_token")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(AuthError):
            await token_info("tok", http=http)


def test_scopes_of_handles_empty() -> None:
    assert scopes_of({}) == set()


def test_quota_project_defaults_to_none() -> None:
    assert StaticTokenProvider("t").quota_project_id is None
    assert ADCAuthProvider().quota_project_id is None  # no creds loaded yet
