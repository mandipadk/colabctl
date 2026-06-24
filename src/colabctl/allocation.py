"""Bounded (re-)allocation — keep auto-resume / re-assign from becoming a cost-runaway.

The detached-job backend (:mod:`colabctl.jobs.backend`) and the interactive lifecycle
manager (:mod:`colabctl.lifecycle`) both re-allocate a GPU when a runtime is reclaimed.
Unbounded, a runtime that *flaps* — reclaimed immediately on every attempt — turns that
into an infinite re-allocation loop, allocating (and billing) a fresh paid GPU each cycle.
That is the single worst footgun for the autonomous-agent use case the project targets.

This module centralises the bound so it is written and tested once:

* a hard **attempt cap** (raise rather than re-allocate past it), and
* **exponential backoff** between attempts (so even within the cap a flap can't hammer
  the allocator), with the sleep injected so it is deterministic in tests.

The cross-backend dollar **budget** (plan Phase 2) will hang off the same gate; today it
only enforces the cap + backoff, which is the safety-critical subset.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from colabctl.errors import AllocationError
from colabctl.observability import get_logger

_log = get_logger("allocation")

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE = 2.0  # seconds
DEFAULT_BACKOFF_MAX = 60.0  # seconds


def backoff_delay(
    attempt: int, *, base: float = DEFAULT_BACKOFF_BASE, cap: float = DEFAULT_BACKOFF_MAX
) -> float:
    """Seconds to wait before the ``attempt``-th (1-based) allocation.

    ``0`` for the first attempt, then exponential (``base``, ``2*base``, ``4*base`` …)
    capped at ``cap``. No jitter — callers wanting it inject a jittering ``sleep``;
    keeping this pure and deterministic makes the bound unit-testable.
    """
    if attempt <= 1:
        return 0.0
    return min(cap, base * 2.0 ** (attempt - 2))


async def _default_sleep(delay: float) -> None:
    await asyncio.sleep(delay)


@dataclass
class AllocationGate:
    """Bounds repeated (re-)allocation for one logical unit of work (a job or a session).

    :meth:`before_attempt` is called *before* each (re-)allocation: it raises
    :class:`~colabctl.errors.AllocationError` once ``attempt`` exceeds ``max_attempts``,
    and otherwise waits the backoff for that attempt. :meth:`backoff` is the wait-only
    half, for callers that already enforce their own cap (e.g. the lifecycle manager).
    ``sleep`` is injectable so tests run instantly and assert the delays.
    """

    backoff_base: float = DEFAULT_BACKOFF_BASE
    backoff_max: float = DEFAULT_BACKOFF_MAX
    sleep: Callable[[float], Awaitable[None]] = _default_sleep
    #: A cumulative USD budget over the spend ledger. None = no dollar cap (only the attempt
    #: cap + backoff apply). Enforced FAIL-CLOSED by :meth:`authorize`.
    budget_usd: float | None = None

    def authorize(
        self,
        *,
        rate_usd_hr: float,
        spent_usd: float = 0.0,
        est_hours: float | None = None,
        max_price_usd_hr: float | None = None,
        what: str,
    ) -> float:
        """Fail-closed budget check before a (re-)allocation; returns the estimated cost.

        Two ceilings, both *guarantees* not preferences (OpenRouter ``max_price`` semantics —
        when in doubt, refuse, never silently pick a pricier option):

        * **Per-job ceiling** — refuse if ``rate_usd_hr`` exceeds ``max_price_usd_hr``.
        * **Cumulative budget** — refuse if ``spent_usd`` plus this allocation's estimated cost
          would exceed ``budget_usd``. ``spent_usd`` comes from the persisted spend ledger, so a
          restart/auto-resume can't reset cumulative spend and bypass the cap.

        Free rates (``0.0``, e.g. Colab/Kaggle) always pass.
        """
        if max_price_usd_hr is not None and rate_usd_hr > max_price_usd_hr:
            raise AllocationError(
                f"{what}: ${rate_usd_hr:.2f}/hr exceeds the per-job cap of "
                f"${max_price_usd_hr:.2f}/hr; refusing to launch (no cheaper backend qualifies)."
            )
        est = rate_usd_hr * (est_hours if est_hours is not None else 1.0)
        if self.budget_usd is not None and spent_usd + est > self.budget_usd:
            raise AllocationError(
                f"{what}: this allocation (~${est:.2f}) would push spend to "
                f"${spent_usd + est:.2f}, over the ${self.budget_usd:.2f} budget; refusing."
            )
        return est

    async def before_attempt(self, attempt: int, max_attempts: int, *, what: str) -> None:
        """Gate the ``attempt``-th (1-based) allocation for ``what``; raise if exhausted."""
        if attempt > max_attempts:
            raise AllocationError(
                f"{what}: refusing to re-allocate — exceeded the cap of {max_attempts} "
                "allocation attempts. A runtime reclaimed immediately on every attempt "
                "signals a broken checkpoint or a flapping backend, not a transient blip; "
                "raise the cap explicitly only if this is genuinely intended."
            )
        await self.backoff(attempt, what=what)

    async def backoff(self, attempt: int, *, what: str) -> None:
        """Wait the exponential backoff for the ``attempt``-th allocation (no-op for #1)."""
        delay = backoff_delay(attempt, base=self.backoff_base, cap=self.backoff_max)
        if delay > 0:
            _log.info("%s: backing off %.1fs before allocation attempt %d", what, delay, attempt)
            await self.sleep(delay)


__all__ = [
    "DEFAULT_BACKOFF_BASE",
    "DEFAULT_BACKOFF_MAX",
    "DEFAULT_MAX_ATTEMPTS",
    "AllocationGate",
    "backoff_delay",
]
