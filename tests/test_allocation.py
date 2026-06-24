"""The AllocationGate: bounded (re-)allocation with exponential backoff.

The safety primitive behind Phase-0.2 — proves the cap raises and the backoff schedule is
what the resume/reassign paths rely on to not become a GPU cost-runaway.
"""

from __future__ import annotations

import pytest

from colabctl.allocation import DEFAULT_BACKOFF_MAX, AllocationGate, backoff_delay
from colabctl.errors import AllocationError


def test_backoff_delay_schedule():
    assert backoff_delay(1) == 0.0  # first attempt never waits
    assert backoff_delay(2, base=2.0) == 2.0
    assert backoff_delay(3, base=2.0) == 4.0
    assert backoff_delay(4, base=2.0) == 8.0
    assert backoff_delay(100, base=2.0, cap=DEFAULT_BACKOFF_MAX) == DEFAULT_BACKOFF_MAX


async def test_before_attempt_allows_within_cap_then_raises():
    slept: list[float] = []

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    gate = AllocationGate(sleep=fake_sleep)  # default base=2.0

    await gate.before_attempt(1, 3, what="job x")  # attempt 1 → no backoff
    await gate.before_attempt(2, 3, what="job x")  # attempt 2 → 2s
    await gate.before_attempt(3, 3, what="job x")  # attempt 3 → 4s
    with pytest.raises(AllocationError, match="exceeded the cap of 3"):
        await gate.before_attempt(4, 3, what="job x")

    # Only the >1 attempts wait; attempt 1 appends nothing.
    assert slept == [2.0, 4.0]


async def test_backoff_only_waits_after_first_attempt():
    slept: list[float] = []

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    gate = AllocationGate(backoff_base=5.0, sleep=fake_sleep)
    await gate.backoff(1, what="s")  # no wait for the first
    assert slept == []
    await gate.backoff(2, what="s")
    assert slept == [5.0]


async def test_zero_base_disables_backoff():
    slept: list[float] = []

    async def fake_sleep(d: float) -> None:  # pragma: no cover - should never be called
        slept.append(d)

    gate = AllocationGate(backoff_base=0.0, sleep=fake_sleep)
    for attempt in range(1, 6):
        await gate.before_attempt(attempt, 10, what="s")
    assert slept == []  # base 0 → every delay is 0 → sleep never invoked


def test_authorize_per_job_ceiling_is_fail_closed():
    gate = AllocationGate()
    # at/under the cap → allowed, returns the estimated cost
    assert gate.authorize(rate_usd_hr=2.0, est_hours=3.0, max_price_usd_hr=3.0, what="job") == 6.0
    # over the cap → refuse (a guarantee, not a preference)
    with pytest.raises(AllocationError, match="exceeds the per-job cap"):
        gate.authorize(rate_usd_hr=4.0, max_price_usd_hr=3.0, what="job")


def test_authorize_cumulative_budget_is_fail_closed():
    gate = AllocationGate(budget_usd=10.0)
    # already spent 8, this would add 1 → 9 ≤ 10 → ok
    assert gate.authorize(rate_usd_hr=1.0, est_hours=1.0, spent_usd=8.0, what="job") == 1.0
    # already spent 9.5, this adds 1 → 10.5 > 10 → refuse
    with pytest.raises(AllocationError, match=r"over the .* budget"):
        gate.authorize(rate_usd_hr=1.0, est_hours=1.0, spent_usd=9.5, what="job")


def test_authorize_free_rate_always_passes():
    gate = AllocationGate(budget_usd=0.0)  # zero budget
    # a free backend ($0/hr, e.g. Colab/Kaggle) never trips the cap, even at budget 0
    assert gate.authorize(rate_usd_hr=0.0, spent_usd=0.0, max_price_usd_hr=0.0, what="job") == 0.0


def test_authorize_defaults_to_one_hour_when_no_estimate():
    gate = AllocationGate()
    assert gate.authorize(rate_usd_hr=2.5, what="job") == 2.5  # est_hours defaults to 1.0
