"""Tests for the @remote marshalling helpers and decorator orchestration."""

from __future__ import annotations

import base64

import cloudpickle
import pytest

from colabctl.errors import SerializationError
from colabctl.sdk import ColabClient, remote
from colabctl.sdk.remote import (
    RESULT_BEGIN,
    RESULT_END,
    build_remote_harness,
    decode_result,
    encode_call,
    parse_result_payload,
)
from conftest import FakeTransport


def _double(x: int) -> int:
    return x * 2


def test_encode_call_roundtrips_via_cloudpickle():
    payload = encode_call(_double, (21,), {})
    fn, args, kwargs = cloudpickle.loads(base64.b64decode(payload))
    assert fn(*args, **kwargs) == 42


def test_build_remote_harness_embeds_payload_and_markers():
    harness = build_remote_harness("UEFZTE9BRA==")
    assert "UEFZTE9BRA==" in harness
    assert "cloudpickle" in harness
    assert RESULT_BEGIN in harness
    assert "base64.b64encode" in harness


def test_parse_and_decode_result_roundtrip():
    value = {"device": "Tesla T4", "ok": True}
    encoded = base64.b64encode(cloudpickle.dumps(value)).decode()
    text = f"some logs\n{RESULT_BEGIN}{encoded}{RESULT_END}\nmore logs\n"
    assert parse_result_payload(text) == cloudpickle.dumps(value)
    assert decode_result(text) == value


def test_parse_result_payload_missing_markers():
    with pytest.raises(SerializationError):
        parse_result_payload("no markers")


async def test_remote_decorator_orchestration_returns_decoded_value():
    # Simulate the VM having produced a pickled result of 99.
    encoded = base64.b64encode(cloudpickle.dumps(99)).decode()
    text = f"{RESULT_BEGIN}{encoded}{RESULT_END}"
    transport = FakeTransport(execute_text=text)
    client = ColabClient(transport=transport)

    @remote(client=client, gpu="T4")
    def compute() -> int:
        return 1  # body irrelevant; the fake returns the pre-baked 99

    assert await compute.aio() == 99
    # Session was allocated and released (owns=True since keep is False).
    assert transport.stopped, "remote() should stop the session it owns"
    # A harness (not the literal body) was sent to the kernel.
    assert transport.executed and "cloudpickle" in transport.executed[0][1]


async def test_remote_decorator_can_keep_session():
    encoded = base64.b64encode(cloudpickle.dumps("done")).decode()
    transport = FakeTransport(execute_text=f"{RESULT_BEGIN}{encoded}{RESULT_END}")
    client = ColabClient(transport=transport)

    @remote(client=client, keep=True)
    def f() -> str:
        return "x"

    assert await f.aio() == "done"
    assert transport.stopped == []  # keep=True leaves the runtime up
