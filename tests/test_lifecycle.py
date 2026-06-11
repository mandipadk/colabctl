"""Tests for the runtime-lifecycle manager: recovery, re-assign+restore, keep-alive tick."""

from __future__ import annotations

import pytest

from colabctl.errors import RuntimeUnavailableError
from colabctl.lifecycle import RuntimeLifecycleManager
from colabctl.models import RuntimeSpec
from conftest import FakeTransport


class CountingTransport(FakeTransport):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.allocate_count = 0

    async def allocate(self, spec):
        self.allocate_count += 1
        return await super().allocate(spec)


class FlakyTransport(CountingTransport):
    """Raises RuntimeUnavailableError on the first ``fail_times`` execute calls."""

    def __init__(self, fail_times=1):
        super().__init__()
        self._fail = fail_times

    async def execute(self, name, code, *, timeout=None, on_output=None):
        if self._fail > 0:
            self._fail -= 1
            raise RuntimeUnavailableError("runtime reclaimed")
        return await super().execute(name, code, timeout=timeout, on_output=on_output)


class AlwaysFailTransport(CountingTransport):
    async def execute(self, name, code, *, timeout=None, on_output=None):
        raise RuntimeUnavailableError("runtime gone")


class ExpiringTransport(CountingTransport):
    """Reports a fixed remaining-time until proxy-token expiry."""

    def __init__(self, remaining):
        super().__init__()
        self._remaining = remaining

    def seconds_until_proxy_expiry(self, name):
        return self._remaining


class RefreshableTransport(ExpiringTransport):
    """Near-expiry, but can refresh the proxy token in place (§5.10)."""

    def __init__(self, remaining, *, refresh_ok=True):
        super().__init__(remaining)
        self.refreshes = 0
        self._refresh_ok = refresh_ok

    async def refresh_token(self, name):
        self.refreshes += 1
        return self._refresh_ok


async def test_allocates_on_start_stops_on_exit():
    t = FakeTransport()
    async with RuntimeLifecycleManager(t, RuntimeSpec(name="lj"), keepalive_interval=0) as mgr:
        assert mgr.name == "lj"
    assert t.stopped == ["lj"]


async def test_execute_recovers_and_restores():
    restored: list[str] = []

    async def restore(transport, name):
        restored.append(name)

    t = FlakyTransport(fail_times=1)
    mgr = RuntimeLifecycleManager(t, RuntimeSpec(name="lj"), keepalive_interval=0, restore=restore)
    await mgr.start()
    result = await mgr.execute("print(1)")
    assert result.ok
    assert t.allocate_count == 2  # initial allocate + one re-assign
    assert restored == ["lj"]  # restore hook ran on the fresh runtime
    assert mgr.reassign_count == 1
    await mgr.stop()


async def test_execute_raises_when_unrecoverable():
    t = AlwaysFailTransport()
    mgr = RuntimeLifecycleManager(t, RuntimeSpec(name="lj"), keepalive_interval=0)
    await mgr.start()
    with pytest.raises(RuntimeUnavailableError):
        await mgr.execute("x=1")
    await mgr.stop()


async def test_keepalive_tick_pings_and_checkpoints():
    checkpoints: list[str] = []

    async def checkpoint(transport, name):
        checkpoints.append(name)

    t = FakeTransport()
    mgr = RuntimeLifecycleManager(
        t, RuntimeSpec(name="lj"), keepalive_interval=0, checkpoint=checkpoint
    )
    await mgr.start()
    await mgr._keepalive_tick()
    assert t.keepalives == ["lj"]  # best-effort activity ping
    assert checkpoints == ["lj"]  # proactive checkpoint
    await mgr.stop()


async def test_checkpoint_now():
    checkpoints: list[str] = []

    async def checkpoint(transport, name):
        checkpoints.append(name)

    t = FakeTransport()
    mgr = RuntimeLifecycleManager(
        t, RuntimeSpec(name="lj"), keepalive_interval=0, checkpoint=checkpoint
    )
    await mgr.start()
    await mgr.checkpoint_now()
    assert checkpoints == ["lj"]
    await mgr.stop()


async def test_on_reassign_observer_fires():
    events: list[tuple[str, str]] = []
    t = FlakyTransport(fail_times=1)
    mgr = RuntimeLifecycleManager(
        t,
        RuntimeSpec(name="lj"),
        keepalive_interval=0,
        on_reassign=lambda name, reason: events.append((name, reason)),
    )
    await mgr.start()
    await mgr.execute("print(1)")
    assert len(events) == 1
    assert events[0][0] == "lj"
    await mgr.stop()


async def test_proactive_reassign_before_expiry():
    restored: list[str] = []

    async def restore(transport, name):
        restored.append(name)

    t = ExpiringTransport(remaining=10.0)  # below the 120s margin
    mgr = RuntimeLifecycleManager(
        t,
        RuntimeSpec(name="lj"),
        keepalive_interval=0,
        reassign_before_expiry=True,
        restore=restore,
    )
    await mgr.start()
    assert t.allocate_count == 1
    await mgr._keepalive_tick()  # sees near-expiry → re-assigns
    assert mgr.reassign_count == 1
    assert t.allocate_count == 2
    assert restored == ["lj"]
    await mgr.stop()


async def test_no_proactive_reassign_when_disabled():
    t = ExpiringTransport(remaining=10.0)
    mgr = RuntimeLifecycleManager(
        t, RuntimeSpec(name="lj"), keepalive_interval=0, reassign_before_expiry=False
    )
    await mgr.start()
    await mgr._keepalive_tick()
    assert mgr.reassign_count == 0  # disabled → reactive-only
    await mgr.stop()


async def test_refresh_before_expiry_prefers_in_place_refresh():
    t = RefreshableTransport(remaining=10.0)  # below the 120s margin
    mgr = RuntimeLifecycleManager(
        t, RuntimeSpec(name="lj"), keepalive_interval=0, refresh_before_expiry=True
    )
    await mgr.start()
    await mgr._keepalive_tick()
    assert t.refreshes == 1  # renewed the token in place
    assert mgr.reassign_count == 0  # and did NOT disrupt the runtime
    await mgr.stop()


async def test_refresh_falls_back_to_reassign_when_unavailable():
    # No refresh_token on the transport → refresh can't run; with reassign also enabled,
    # the manager falls back to the disruptive path rather than ignoring expiry.
    t = ExpiringTransport(remaining=10.0)
    mgr = RuntimeLifecycleManager(
        t,
        RuntimeSpec(name="lj"),
        keepalive_interval=0,
        refresh_before_expiry=True,
        reassign_before_expiry=True,
    )
    await mgr.start()
    await mgr._keepalive_tick()
    assert mgr.reassign_count == 1  # fell back to re-assign
    await mgr.stop()
