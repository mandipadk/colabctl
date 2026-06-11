"""Probe-based reclaim detection (§5.4) + keep-alive tick hardening (§5.5).

The lifecycle manager must not destroy a warm runtime on a transient transport blip:
when the transport exposes ``is_live``, ambiguous errors are probed and retried in
place; only confirmed reclamation re-assigns. ``RuntimeUnavailableError`` stays a
definite signal, and transports without a probe keep the old re-assign behavior
(covered by test_lifecycle.py).
"""

from __future__ import annotations

import pytest

from colabctl.errors import RuntimeUnavailableError, TransportError
from colabctl.lifecycle import RuntimeLifecycleManager
from colabctl.models import RuntimeSpec
from conftest import FakeTransport


class ProbeTransport(FakeTransport):
    """FakeTransport with an ``is_live`` probe and scriptable execute failures.

    ``fail_with`` errors are raised by successive execute calls (then it succeeds);
    ``live`` scripts successive probe answers (last value repeats).
    """

    def __init__(self, *, fail_with: list[Exception] | None = None, live: list[bool] | None = None):
        super().__init__()
        self.allocate_count = 0
        self.probe_calls = 0
        self._failures = list(fail_with or [])
        self._live = list(live or [True])

    async def allocate(self, spec):
        self.allocate_count += 1
        return await super().allocate(spec)

    async def is_live(self, name: str) -> bool:
        self.probe_calls += 1
        return self._live.pop(0) if len(self._live) > 1 else self._live[0]

    async def execute(self, name, code, *, timeout=None, on_output=None):
        if self._failures:
            raise self._failures.pop(0)
        return await super().execute(name, code, timeout=timeout, on_output=on_output)


async def test_transient_error_with_live_runtime_retries_in_place():
    t = ProbeTransport(fail_with=[TransportError("blip")], live=[True])
    mgr = RuntimeLifecycleManager(t, RuntimeSpec(name="lj"), keepalive_interval=0)
    await mgr.start()
    result = await mgr.execute("print(1)")
    assert result.ok
    assert t.allocate_count == 1  # warm runtime preserved — no re-assign
    assert mgr.reassign_count == 0
    assert t.probe_calls == 1
    await mgr.stop()


async def test_transport_error_with_dead_runtime_reassigns():
    restored: list[str] = []

    async def restore(transport, name):
        restored.append(name)

    t = ProbeTransport(fail_with=[TransportError("conn reset")], live=[False])
    mgr = RuntimeLifecycleManager(t, RuntimeSpec(name="lj"), keepalive_interval=0, restore=restore)
    await mgr.start()
    result = await mgr.execute("print(1)")
    assert result.ok
    assert t.allocate_count == 2  # probe confirmed reclaim → re-assign
    assert restored == ["lj"]
    await mgr.stop()


async def test_persistent_error_on_live_runtime_is_surfaced_not_reassigned():
    # Two failures while the runtime stays live: the error is real (not reclamation);
    # surfacing it beats silently destroying the runtime and re-running the code.
    t = ProbeTransport(
        fail_with=[TransportError("err1"), TransportError("err2")], live=[True, True]
    )
    mgr = RuntimeLifecycleManager(t, RuntimeSpec(name="lj"), keepalive_interval=0)
    await mgr.start()
    with pytest.raises(TransportError, match="err2"):
        await mgr.execute("x=1")
    assert mgr.reassign_count == 0
    assert t.allocate_count == 1
    await mgr.stop()


async def test_runtime_died_between_retry_and_probe_reassigns():
    # First probe says live (retry in place), retry fails, second probe says dead.
    t = ProbeTransport(
        fail_with=[TransportError("err1"), TransportError("err2")], live=[True, False]
    )
    mgr = RuntimeLifecycleManager(t, RuntimeSpec(name="lj"), keepalive_interval=0)
    await mgr.start()
    result = await mgr.execute("print(1)")
    assert result.ok
    assert mgr.reassign_count == 1
    assert t.allocate_count == 2
    await mgr.stop()


async def test_runtime_unavailable_is_definite_no_probe_needed():
    t = ProbeTransport(fail_with=[RuntimeUnavailableError("reclaimed")], live=[True])
    mgr = RuntimeLifecycleManager(t, RuntimeSpec(name="lj"), keepalive_interval=0)
    await mgr.start()
    result = await mgr.execute("print(1)")
    assert result.ok
    assert mgr.reassign_count == 1  # trusted the transport's definite signal
    assert t.probe_calls == 0  # no probe — would be wasted work
    await mgr.stop()


async def test_probe_failure_falls_back_to_reassign():
    class BrokenProbe(ProbeTransport):
        async def is_live(self, name: str) -> bool:
            raise OSError("probe transport down")

    t = BrokenProbe(fail_with=[TransportError("blip")])
    mgr = RuntimeLifecycleManager(t, RuntimeSpec(name="lj"), keepalive_interval=0)
    await mgr.start()
    result = await mgr.execute("print(1)")
    assert result.ok
    assert mgr.reassign_count == 1  # cannot tell → old (re-assign) behavior
    await mgr.stop()


# -- keep-alive tick (§5.4 in the loop, §5.5 ping gate) ------------------------


async def test_tick_failure_with_live_runtime_does_not_reassign():
    class FailingKeepAlive(ProbeTransport):
        async def keep_alive(self, name: str) -> None:
            raise TransportError("ping timed out (kernel busy)")

    t = FailingKeepAlive(live=[True])
    mgr = RuntimeLifecycleManager(t, RuntimeSpec(name="lj"), keepalive_interval=0)
    await mgr.start()
    # Drive the loop body once via the tick + the loop's error handling contract:
    with pytest.raises(TransportError):
        await mgr._keepalive_tick()
    assert (await t.is_live("lj")) is True
    assert mgr.reassign_count == 0
    await mgr.stop()


async def test_ping_gate_skips_ping_but_still_checkpoints():
    checkpoints: list[str] = []

    async def checkpoint(transport, name):
        checkpoints.append(name)

    t = FakeTransport()
    mgr = RuntimeLifecycleManager(
        t,
        RuntimeSpec(name="lj"),
        keepalive_interval=0,
        checkpoint=checkpoint,
        ping_gate=lambda: False,  # e.g. a detached job is keeping the kernel busy
    )
    await mgr.start()
    await mgr._keepalive_tick()
    assert t.keepalives == []  # ping skipped
    assert checkpoints == ["lj"]  # checkpoint still ran
    await mgr.stop()


async def test_ping_gate_true_pings_normally():
    t = FakeTransport()
    mgr = RuntimeLifecycleManager(
        t, RuntimeSpec(name="lj"), keepalive_interval=0, ping_gate=lambda: True
    )
    await mgr.start()
    await mgr._keepalive_tick()
    assert t.keepalives == ["lj"]
    await mgr.stop()
