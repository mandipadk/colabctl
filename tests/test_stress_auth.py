"""Adversarial tests for ADCAuthProvider token refresh/caching/error handling."""

from __future__ import annotations

import google.auth
import pytest

from colabctl.auth.adc import ADCAuthProvider
from colabctl.errors import AuthError


class FakeCreds:
    def __init__(
        self,
        *,
        valid=True,
        token="tok",
        requires_scopes=False,
        refresh_fails=False,
        service_account_email=None,
    ):
        self.valid = valid
        self.token = token
        self.requires_scopes = requires_scopes
        self._refresh_fails = refresh_fails
        self.refreshed = False
        self.scoped = False
        if service_account_email is not None:
            self.service_account_email = service_account_email

    def with_scopes(self, scopes):
        self.scoped = True
        return self

    def refresh(self, request):
        if self._refresh_fails:
            raise RuntimeError("refresh boom")
        self.valid = True
        self.token = "refreshed-tok"
        self.refreshed = True


def _patch_default(monkeypatch, creds, counter=None):
    def fake_default(scopes=None):
        if counter is not None:
            counter["n"] += 1
        return creds, None

    monkeypatch.setattr(google.auth, "default", fake_default)


async def test_valid_creds_return_token_without_refresh(monkeypatch):
    creds = FakeCreds(valid=True, token="tok")
    _patch_default(monkeypatch, creds)
    assert await ADCAuthProvider().token() == "tok"
    assert creds.refreshed is False


async def test_invalid_creds_are_refreshed(monkeypatch):
    creds = FakeCreds(valid=False, token="stale")
    _patch_default(monkeypatch, creds)
    assert await ADCAuthProvider().token() == "refreshed-tok"
    assert creds.refreshed is True


async def test_refresh_failure_raises_auth_error(monkeypatch):
    creds = FakeCreds(valid=False, refresh_fails=True)
    _patch_default(monkeypatch, creds)
    with pytest.raises(AuthError, match="refresh"):
        await ADCAuthProvider().token()


async def test_empty_token_raises_auth_error(monkeypatch):
    creds = FakeCreds(valid=True, token="")
    _patch_default(monkeypatch, creds)
    with pytest.raises(AuthError, match="no access token"):
        await ADCAuthProvider().token()


async def test_requires_scopes_applies_with_scopes(monkeypatch):
    creds = FakeCreds(valid=True, token="tok", requires_scopes=True)
    _patch_default(monkeypatch, creds)
    await ADCAuthProvider().token()
    assert creds.scoped is True


async def test_credentials_are_cached(monkeypatch):
    counter = {"n": 0}
    creds = FakeCreds(valid=True, token="tok")
    _patch_default(monkeypatch, creds, counter)
    provider = ADCAuthProvider()
    await provider.token()
    await provider.token()
    assert counter["n"] == 1  # google.auth.default called once, then cached


async def test_email_best_effort(monkeypatch):
    creds = FakeCreds(valid=True, token="tok", service_account_email="svc@example.com")
    _patch_default(monkeypatch, creds)
    provider = ADCAuthProvider()
    await provider.token()
    assert await provider.email() == "svc@example.com"


async def test_email_none_before_token(monkeypatch):
    assert await ADCAuthProvider().email() is None  # no creds yet


async def test_as_token_callable_round_trips(monkeypatch):
    creds = FakeCreds(valid=True, token="tok")
    _patch_default(monkeypatch, creds)
    provider = ADCAuthProvider()
    fn = provider.as_token_callable()
    assert await fn() == "tok"
