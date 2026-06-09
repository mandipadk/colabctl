"""Tests for retry/backoff + the spend-guard timeout cap."""

from __future__ import annotations

import pytest

from colabctl.errors import QuotaExceededError, TransportError
from colabctl.observability import cap_timeout, retry_async


def test_cap_timeout():
    assert cap_timeout(100, maximum=3600) == 100
    assert cap_timeout(99999, maximum=3600) == 3600
    assert cap_timeout(3600, maximum=3600) == 3600


async def test_retry_succeeds_first_try():
    calls = []
    delays = []

    async def op():
        calls.append(1)
        return "ok"

    async def sleep(d):
        delays.append(d)

    assert await retry_async(op, sleep=sleep, jitter=lambda: 0.0) == "ok"
    assert len(calls) == 1
    assert delays == []  # no backoff on success


async def test_retry_then_succeed_uses_exponential_backoff():
    state = {"n": 0}
    delays = []

    async def op():
        state["n"] += 1
        if state["n"] < 3:
            raise TransportError("flaky")
        return "ok"

    async def sleep(d):
        delays.append(d)

    result = await retry_async(op, retries=3, base_delay=1.0, sleep=sleep, jitter=lambda: 0.0)
    assert result == "ok"
    assert delays == [1.0, 2.0]


async def test_retry_exhausts_and_raises():
    async def op():
        raise TransportError("always")

    async def sleep(d):
        pass

    with pytest.raises(TransportError):
        await retry_async(op, retries=2, sleep=sleep, jitter=lambda: 0.0)


async def test_retry_gives_up_immediately_on_terminal_error():
    calls = []

    async def op():
        calls.append(1)
        raise QuotaExceededError("quota exhausted")

    async def sleep(d):
        pass

    with pytest.raises(QuotaExceededError):
        await retry_async(op, sleep=sleep, jitter=lambda: 0.0)
    assert len(calls) == 1  # NON_RETRYABLE → no backoff


async def test_retry_caps_delay_at_max():
    state = {"n": 0}
    delays = []

    async def op():
        state["n"] += 1
        if state["n"] < 5:
            raise TransportError("x")
        return "ok"

    async def sleep(d):
        delays.append(d)

    await retry_async(
        op, retries=10, base_delay=1.0, max_delay=3.0, sleep=sleep, jitter=lambda: 0.0
    )
    assert delays == [1.0, 2.0, 3.0, 3.0]  # capped at max_delay
