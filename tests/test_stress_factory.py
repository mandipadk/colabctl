"""Adversarial tests for backend factory dispatch and the native opt-in gate."""

from __future__ import annotations

import pytest

from colabctl.backends.factory import BACKEND_NAMES, build_backend, build_router
from colabctl.errors import ConfigurationError
from colabctl.transport.native.adapter import native_opt_in_enabled, require_native_opt_in

# --- factory dispatch -------------------------------------------------------


def test_backend_names_are_expected():
    assert BACKEND_NAMES == ("colab", "modal", "vertex", "hf", "kaggle", "runpod", "vast")


@pytest.mark.parametrize("name", ["modal", "vertex", "hf", "kaggle", "runpod", "vast"])
def test_build_backend_known_non_colab(name):
    # These construct cheaply (config only, no SDK import / network).
    assert build_backend(name).name == name


@pytest.mark.parametrize("name", ["MODAL", "Vertex", "Hf", "KAGGLE", "RunPod"])
def test_build_backend_is_case_insensitive(name):
    assert build_backend(name).name == name.lower()


def test_build_backend_unknown_raises_and_lists_choices():
    with pytest.raises(ConfigurationError) as ei:
        build_backend("nonsense")
    msg = str(ei.value)
    for n in BACKEND_NAMES:
        assert n in msg


def test_build_router_preserves_order_and_dedups():
    r = build_router(["modal", "vertex", "modal"])
    assert r._order == ["modal", "vertex"]  # duplicate collapsed
    assert set(r._backends.keys()) == {"modal", "vertex"}


def test_build_router_unknown_name_raises():
    with pytest.raises(ConfigurationError):
        build_router(["modal", "ghost"])


# --- native opt-in gate -----------------------------------------------------


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "Yes", "on", " on ", "yes"])
def test_native_enabled_truthy(monkeypatch, val):
    monkeypatch.setenv("COLABCTL_ENABLE_NATIVE", val)
    assert native_opt_in_enabled() is True
    require_native_opt_in()  # must not raise


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "2", "enabled", "  "])
def test_native_disabled_falsy(monkeypatch, val):
    monkeypatch.setenv("COLABCTL_ENABLE_NATIVE", val)
    assert native_opt_in_enabled() is False
    with pytest.raises(ConfigurationError):
        require_native_opt_in()


def test_native_disabled_when_unset(monkeypatch):
    monkeypatch.delenv("COLABCTL_ENABLE_NATIVE", raising=False)
    assert native_opt_in_enabled() is False
    with pytest.raises(ConfigurationError):
        require_native_opt_in()
