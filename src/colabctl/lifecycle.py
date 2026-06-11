"""Runtime-lifecycle manager for long-running Colab sessions.

This is the honest answer to the Phase 0 keep-alive finding: there is no reliable
token-auth keep-alive RPC, so a durable long-running session is achieved by

1. **best-effort keep-alive ticks** — periodic kernel activity (where the transport
   supports it), to defer idle reclamation while a workload is running;
2. **proactive checkpoints** — a user-supplied hook runs periodically against the live
   runtime to externalize state (e.g. push to Drive/GCS);
3. **automatic re-assign + restore** — when the runtime is *confirmed* reclaimed,
   allocate a fresh one and run a restore hook, then retry. Ambiguous transport errors
   are probed first (the transport's optional ``is_live``) so a network blip never
   destroys a warm, healthy runtime (§5.4); only a definite or probe-confirmed
   reclamation triggers the disruptive path.

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
#: Gate consulted before each keep-alive activity ping; return ``False`` to skip the
#: ping this tick (e.g. while a detached job already keeps the kernel busy — §5.5).
PingGate = Callable[[], bool]

#: Errors that *may* indicate the runtime is gone. ``RuntimeUnavailableError`` is a
#: definite reclaim signal (the transport says so); the broader transport/allocation
#: errors are ambiguous — a network blip raises the same types — so when the transport
#: can be probed (``is_live``) we check before destroying a warm runtime (plan §5.4).
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
        refresh_before_expiry: bool = False,
        reassign_before_expiry: bool = False,
        expiry_margin: float = 120.0,
        ping_gate: PingGate | None = None,
    ) -> None:
        self._transport = transport
        self._spec = spec
        self._keepalive_interval = keepalive_interval
        self._checkpoint = checkpoint
        self._restore = restore
        self._on_reassign = on_reassign
        self._max_reassigns = max_reassigns
        self._ping_gate = ping_gate
        # Near proxy-token expiry, prefer a non-disruptive in-place refresh (transports
        # exposing `refresh_token`, Phase A §②) over a disruptive re-assign. Both default
        # off, since the reactive on-failure path already covers expiry; refresh is the
        # cheap proactive option and is tried first when enabled.
        self._refresh_before_expiry = refresh_before_expiry
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

    async def _runtime_is_live(self) -> bool | None:
        """Probe whether the current runtime still exists (``None`` = cannot tell).

        Uses the transport's optional ``is_live`` (the native transport checks the
        server's assignment list). A missing probe, a probe error, or no active
        session all return ``None`` — in which case recovery falls back to the
        probe-less behavior (re-assign).
        """
        if self._name is None:
            return False
        probe = getattr(self._transport, "is_live", None)
        if probe is None:
            return None
        try:
            result: bool | None = await probe(self._name)
        except Exception:  # probe is advisory; its own failure must not mask the error
            return None
        return result

    async def _with_recovery(self, op: Callable[[], Awaitable[_T]]) -> _T:
        """Run ``op``, recovering from reclamation — but never from a mere blip.

        - ``RuntimeUnavailableError`` is the transport's definite "runtime gone"
          signal → re-assign + restore + retry.
        - Broader transport/allocation errors are ambiguous: when the transport can be
          probed and the runtime is still live, retry **in place** (the warm runtime —
          and everything on it — is preserved). If the retry fails again while the
          runtime remains live, the error is real and is surfaced, not "recovered"
          into a destructive re-assign.
        - Without a probe, fall back to re-assign (the pre-§5.4 behavior).
        """
        try:
            return await op()
        except RuntimeUnavailableError as exc:
            await self._reassign(reason=f"{type(exc).__name__}: {exc}")
            return await op()  # retry once on the fresh runtime
        except _RECLAIM_ERRORS as exc:
            if await self._runtime_is_live():
                _log.warning(
                    "recovery: runtime still live after %s; retrying in place", type(exc).__name__
                )
                try:
                    return await op()
                except _RECLAIM_ERRORS as exc2:
                    if await self._runtime_is_live():
                        raise  # runtime is healthy — this is a real error, surface it
                    await self._reassign(reason=f"{type(exc2).__name__}: {exc2}")
                    return await op()
            await self._reassign(reason=f"{type(exc).__name__}: {exc}")
            return await op()

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
        """One keep-alive cycle: activity ping, optional checkpoint, expiry check.

        The ping is skipped when the ping gate says so (§5.5: a detached job already
        keeps the kernel busy, and its poller touches the kernel anyway); checkpoints
        and the expiry check still run.
        """
        if self._name is None:
            return
        keep_alive = getattr(self._transport, "keep_alive", None)
        if keep_alive is not None and (self._ping_gate is None or self._ping_gate()):
            await keep_alive(self._name)
        if self._checkpoint is not None:
            await self._checkpoint(self._transport, self._name)
        if self._proxy_expiring_soon():
            if self._refresh_before_expiry and await self._try_refresh_token():
                _log.info("keepalive: refreshed proxy token in place near expiry")
            elif self._reassign_before_expiry:
                await self._reassign(reason="runtime-proxy token near expiry")

    async def _try_refresh_token(self) -> bool:
        """Refresh the proxy token in place if the transport supports it (§5.10)."""
        refresh = getattr(self._transport, "refresh_token", None)
        if refresh is None or self._name is None:
            return False
        try:
            return bool(await refresh(self._name))
        except _RECLAIM_ERRORS as exc:
            _log.warning("keepalive: in-place token refresh failed (%s); falling back", exc)
            return False

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
                # Possibly reclaimed between ticks — but a tick can also fail on a
                # transient blip (or a bounded ping timeout against a busy kernel),
                # so probe before the disruptive re-assign (§5.4).
                if await self._runtime_is_live():
                    _log.warning("keepalive: tick failed but runtime is live (%s); skipping", exc)
                    continue
                _log.warning("keepalive: runtime unavailable, re-assigning (%s)", exc)
                with contextlib.suppress(*_RECLAIM_ERRORS):
                    await self._reassign(reason=f"keepalive: {exc}")
            except Exception:
                # Never let the background loop die on an unexpected error (e.g. a
                # bug in a user checkpoint hook), but don't swallow it silently either.
                _log.exception("keepalive: unexpected error in tick; continuing")
