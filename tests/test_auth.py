"""Tests for the auth layer (scopes + static provider + native-client adapter)."""

from __future__ import annotations

from colabctl.auth import (
    ADC_LOGIN_SCOPES,
    COLAB_SCOPES,
    StaticTokenProvider,
)


def test_colab_scopes_include_colaboratory():
    assert "https://www.googleapis.com/auth/colaboratory" in COLAB_SCOPES
    assert "openid" in COLAB_SCOPES


def test_adc_login_scopes_include_cloud_platform():
    # gcloud refuses ADC login without cloud-platform (Phase 0 finding).
    assert "https://www.googleapis.com/auth/cloud-platform" in ADC_LOGIN_SCOPES
    assert "https://www.googleapis.com/auth/colaboratory" in ADC_LOGIN_SCOPES


async def test_static_token_provider():
    provider = StaticTokenProvider("tok-123", email="a@x.com")
    assert await provider.token() == "tok-123"
    assert await provider.email() == "a@x.com"


async def test_as_token_callable_round_trips():
    provider = StaticTokenProvider("tok-xyz")
    callable_ = provider.as_token_callable()
    assert await callable_() == "tok-xyz"
