"""Tests for the backend factory."""

from __future__ import annotations

import pytest

from colabctl.backends import (
    ColabBackend,
    HFJobsBackend,
    ModalBackend,
    VertexBackend,
)
from colabctl.backends.factory import BACKEND_NAMES, build_backend, build_router
from colabctl.errors import ConfigurationError


def test_build_known_backends():
    assert isinstance(build_backend("modal"), ModalBackend)
    assert isinstance(build_backend("vertex"), VertexBackend)
    assert isinstance(build_backend("colab"), ColabBackend)
    assert isinstance(build_backend("hf"), HFJobsBackend)


def test_build_unknown_backend_raises():
    with pytest.raises(ConfigurationError):
        build_backend("bogus")


def test_build_router_orders_by_names():
    router = build_router(["modal", "vertex"])
    assert router.get("modal").name == "modal"
    assert router.get("vertex").name == "vertex"


def test_backend_names_constant():
    assert BACKEND_NAMES == ("colab", "modal", "vertex", "hf")
