"""Adversarial tests: ColabSession __aexit__ exception-safety + lifecycle edges."""

from __future__ import annotations

import asyncio

import pytest

from colabctl.errors import ExecutionError, RuntimeUnavailableError, TransportError
from colabctl.lifecycle import RuntimeLifecycleManager
from colabctl.models import RuntimeSpec
from colabctl.sdk.client import ColabSession
from conftest import FakeTransport


class StopFailsTransport(FakeTransport):
    """stop() records then raises, to probe __aexit__ cleanup behavior."""

    def __init__(self, stop_exc: Exception | None = None) -> None:
        super().__init__()
        self._stop_exc = stop_exc

    async def stop(self, name: str) -> None:
        await super().stop(name)
        if self._stop_exc is not None:
            raise self._stop_exc


# --- ColabSession.__aexit__ exception safety --------------------------------


async def test_aexit_does_not_mask_body_exception():
    t = StopFailsTransport(stop_exc=TransportError("stop blew up"))
    sess = ColabSession(t, "s", owns=True)
    with pytest.raises(ValueError, match="body error"):
        async with sess:
            raise ValueError("body error")
    assert "s" in t.stopped  # cleanup was still attempted (best-effort)


async def test_aexit_stop_error_propagates_on_clean_exit():
    # On a clean exit there's no in-flight exception to mask, so a failed release
    # (a possible cost leak) must surface.
    t = StopFailsTransport(stop_exc=TransportError("stop blew up"))
    sess = ColabSession(t, "s", owns=True)
    with pytest.raises(TransportError):
        async with sess:
            pass


async def test_aexit_not_owned_never_stops():
    t = StopFailsTransport(stop_exc=TransportError("would blow up if called"))
    sess = ColabSession(t, "s", owns=False)
    async with sess:
        pass
    assert t.stopped == []  # kept sessions are never auto-stopped


async def test_aexit_owned_clean_exit_stops():
    t = FakeTransport()
    sess = ColabSession(t, "s", owns=True)
    async with sess:
        pass
    assert t.stopped == ["s"]


# --- lifecycle: only reclaim errors trigger re-assign -----------------------


class ExecErrorTransport(FakeTransport):
    """execute() raises a NON-reclaim error (a user-code failure)."""

    def __init__(self) -> None:
        super().__init__()
        self.execute_calls = 0

    async def execute(self, name, code, *, timeout=None, on_output=None):
        self.execute_calls += 1
        raise ExecutionError("user code blew up", ename="ValueError")


async def test_non_reclaim_error_is_not_retried():
    t = ExecErrorTransport()
    mgr = RuntimeLifecycleManager(t, RuntimeSpec(), keepalive_interval=0)
    await mgr.start()
    with pytest.raises(ExecutionError):
        await mgr.execute("print(1)")
    assert t.execute_calls == 1  # no re-assign, no retry for a code error
    assert mgr.reassign_count == 0
    await mgr.stop()


# --- lifecycle: keepalive loop survives an unexpected hook error ------------


async def test_keepalive_loop_survives_unexpected_hook_error():
    t = FakeTransport()
    ticks = {"n": 0}

    async def flaky_checkpoint(transport, name):
        ticks["n"] += 1
        raise RuntimeError("hook bug")  # NOT a reclaim error

    mgr = RuntimeLifecycleManager(
        t, RuntimeSpec(), keepalive_interval=0.01, checkpoint=flaky_checkpoint
    )
    await mgr.start()
    await asyncio.sleep(0.05)  # let several ticks fire
    # The loop must still be alive (task not done) despite the hook raising.
    assert mgr._ka_task is not None and not mgr._ka_task.done()
    assert ticks["n"] >= 1
    await mgr.stop()
    assert mgr._ka_task is None


# --- lifecycle: stop() is idempotent ----------------------------------------


async def test_stop_is_idempotent():
    t = FakeTransport()
    mgr = RuntimeLifecycleManager(t, RuntimeSpec(), keepalive_interval=0)
    await mgr.start()
    await mgr.stop()
    await mgr.stop()  # second stop must be a no-op, not an error
    with pytest.raises(RuntimeUnavailableError):
        _ = mgr.name  # cleared after stop
