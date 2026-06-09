"""Runtime-lifecycle manager for long-running Colab sessions.

This is the honest answer to the Phase 0 keep-alive finding: there is no reliable
token-auth keep-alive RPC, so a durable long-running session is achieved by

1. **best-effort keep-alive ticks** — periodic kernel activity (where the transport
   supports it), to defer idle reclamation while a workload is running;
2. **proactive checkpoints** — a user-supplied hook runs periodically against the live
   runtime to externalize state (e.g. push to Drive/GCS);
3. **automatic re-assign + restore** — when the runtime is reclaimed (a call raises a
   runtime/transport error), allocate a fresh one and run a restore hook, then retry.

Checkpoint/restore *content* is pluggable (the manager orchestrates; the hooks decide
what to persist). It wraps a :class:`TransportAdapter`, so it works over both the CLI
and native transports.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import TypeVar

from colabctl.errors import (
    AllocationError,
    RuntimeUnavailableError,
    TransportError,
)
from colabctl.models import ExecutionResult, RuntimeSpec, SessionInfo
from colabctl.observability import get_logger
from colabctl.transport.base import OutputCallback, TransportAdapter

_T = TypeVar("_T")

_log = get_logger("lifecycle")

#: A hook invoked with (transport, session_name). Used for checkpoint and restore.
LifecycleHook = Callable[[TransportAdapter, str], Awaitable[None]]
#: Observer invoked on each re-assign with (new_session_name, reason).
ReassignObserver = Callable[[str, str], None]

#: Errors that indicate the runtime is gone and a re-assign should be attempted.
_RECLAIM_ERRORS = (RuntimeUnavailableError, AllocationError, TransportError)


class RuntimeLifecycleManager:
    """Keeps a long-running session alive, re-assigning + restoring on reclamation."""

    def __init__(
        self,
        transport: TransportAdapter,
        spec: RuntimeSpec,
        *,
        keepalive_interval: float = 240.0,
        checkpoint: LifecycleHook | None = None,
        restore: LifecycleHook | None = None,
        on_reassign: ReassignObserver | None = None,
        max_reassigns: int = 3,
        reassign_before_expiry: bool = False,
        expiry_margin: float = 120.0,
    ) -> None:
        self._transport = transport
        self._spec = spec
        self._keepalive_interval = keepalive_interval
        self._checkpoint = checkpoint
        self._restore = restore
        self._on_reassign = on_reassign
        self._max_reassigns = max_reassigns
        # Proactively re-assign before the runtime-proxy token expires (transports that
        # expose `seconds_until_proxy_expiry`); off by default since re-assign is
        # disruptive and the reactive on-failure path already covers expiry.
        self._reassign_before_expiry = reassign_before_expiry
        self._expiry_margin = expiry_margin
        self._name: str | None = None
        self._reassigns = 0
        self._ka_task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        if self._name is None:
            raise RuntimeUnavailableError("Lifecycle manager has no active session; call start().")
        return self._name

    @property
    def reassign_count(self) -> int:
        return self._reassigns

    async def start(self) -> SessionInfo:
        info = await self._allocate()
        if self._keepalive_interval > 0:
            self._ka_task = asyncio.create_task(self._keepalive_loop())
        return info

    async def execute(
        self,
        code: str,
        *,
        timeout: float | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecutionResult:
        """Execute with one automatic re-assign+restore+retry if the runtime is gone."""
        return await self._with_recovery(
            lambda: self._transport.execute(self.name, code, timeout=timeout, on_output=on_output)
        )

    async def checkpoint_now(self) -> None:
        """Run the checkpoint hook against the live runtime, if configured."""
        if self._checkpoint is not None and self._name is not None:
            await self._checkpoint(self._transport, self._name)

    async def stop(self) -> None:
        if self._ka_task is not None:
            self._ka_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ka_task
            self._ka_task = None
        if self._name is not None:
            with contextlib.suppress(*_RECLAIM_ERRORS):
                await self._transport.stop(self._name)
            self._name = None

    async def __aenter__(self) -> RuntimeLifecycleManager:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    # -- internals ----------------------------------------------------------

    async def _allocate(self) -> SessionInfo:
        info = await self._transport.allocate(self._spec)
        self._name = info.name
        return info

    async def _with_recovery(self, op: Callable[[], Awaitable[_T]]) -> _T:
        try:
            return await op()
        except _RECLAIM_ERRORS as exc:
            await self._reassign(reason=f"{type(exc).__name__}: {exc}")
            return await op()  # retry once on the fresh runtime

    async def _reassign(self, *, reason: str) -> None:
        self._reassigns += 1
        if self._reassigns > self._max_reassigns:
            raise RuntimeUnavailableError(
                f"Exceeded max re-assigns ({self._max_reassigns}); last reason: {reason}"
            )
        # The old runtime is presumed gone; best-effort release, then allocate fresh.
        old = self._name
        self._name = None
        if old is not None:
            with contextlib.suppress(*_RECLAIM_ERRORS):
                await self._transport.stop(old)
        await self._allocate()
        if self._restore is not None:
            await self._restore(self._transport, self.name)
        if self._on_reassign is not None:
            self._on_reassign(self.name, reason)

    async def _keepalive_tick(self) -> None:
        """One keep-alive cycle: activity ping, optional checkpoint, expiry check."""
        if self._name is None:
            return
        keep_alive = getattr(self._transport, "keep_alive", None)
        if keep_alive is not None:
            await keep_alive(self._name)
        if self._checkpoint is not None:
            await self._checkpoint(self._transport, self._name)
        if self._reassign_before_expiry and self._proxy_expiring_soon():
            await self._reassign(reason="runtime-proxy token near expiry")

    def _proxy_expiring_soon(self) -> bool:
        expires_in = getattr(self._transport, "seconds_until_proxy_expiry", None)
        if expires_in is None or self._name is None:
            return False
        remaining = expires_in(self._name)
        return remaining is not None and remaining < self._expiry_margin

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(self._keepalive_interval)
            try:
                await self._keepalive_tick()
            except _RECLAIM_ERRORS as exc:
                # Runtime likely reclaimed between ticks — re-assign proactively.
                _log.warning("keepalive: runtime unavailable, re-assigning (%s)", exc)
                with contextlib.suppress(*_RECLAIM_ERRORS):
                    await self._reassign(reason=f"keepalive: {exc}")
            except Exception:
                # Never let the background loop die on an unexpected error (e.g. a
                # bug in a user checkpoint hook), but don't swallow it silently either.
                _log.exception("keepalive: unexpected error in tick; continuing")
