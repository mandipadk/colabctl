"""The native /tun/m/* transport is opt-in / disabled by default (ToS posture)."""

from __future__ import annotations

import pytest

from colabctl.auth import StaticTokenProvider
from colabctl.errors import ConfigurationError
from colabctl.sdk import ColabClient
from colabctl.transport.native.adapter import NativeColabTransport, native_opt_in_enabled


async def test_create_blocked_without_opt_in(monkeypatch):
    monkeypatch.delenv("COLABCTL_ENABLE_NATIVE", raising=False)
    assert native_opt_in_enabled() is False
    with pytest.raises(ConfigurationError):
        NativeColabTransport.create(StaticTokenProvider("tok"))


async def test_create_allowed_with_explicit_flag(monkeypatch):
    monkeypatch.delenv("COLABCTL_ENABLE_NATIVE", raising=False)
    transport = NativeColabTransport.create(StaticTokenProvider("tok"), allow_native=True)
    assert transport.name == "native"
    await transport.aclose()


async def test_create_allowed_with_env(monkeypatch):
    monkeypatch.setenv("COLABCTL_ENABLE_NATIVE", "1")
    assert native_opt_in_enabled() is True
    transport = NativeColabTransport.create(StaticTokenProvider("tok"))
    assert transport.name == "native"
    await transport.aclose()


def test_client_native_transport_blocked_without_opt_in(monkeypatch):
    monkeypatch.delenv("COLABCTL_ENABLE_NATIVE", raising=False)
    with pytest.raises(ConfigurationError):
        ColabClient(transport_name="native")
