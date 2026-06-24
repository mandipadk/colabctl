"""Tests for BackendRouter: capability selection + infra failover."""

from __future__ import annotations

import pytest

from colabctl.backends.base import (
    Backend,
    BackendCapabilities,
    JobInfo,
    JobResult,
    JobSpec,
    JobState,
)
from colabctl.backends.router import BackendRouter
from colabctl.errors import AllocationError, ColabctlError, ConfigurationError
from colabctl.models import Accelerator


class FakeBackend(Backend):
    def __init__(self, name, accels, *, result=None, raises=None):
        self.name = name
        self._accels = accels
        self._result = result
        self._raises = raises
        self.run_calls = 0

    @property
    def capabilities(self):
        return BackendCapabilities(name=self.name, accelerators=self._accels)

    async def run(self, spec):
        self.run_calls += 1
        if self._raises is not None:
            raise self._raises
        return self._result or JobResult(id="x", backend=self.name, state=JobState.SUCCEEDED)

    # unused abstract methods for these tests
    async def submit(self, spec):
        return JobInfo(id="x", backend=self.name, state=JobState.PENDING)

    async def status(self, job_id):
        return JobInfo(id=job_id, backend=self.name, state=JobState.UNKNOWN)

    async def logs(self, job_id):
        return ""

    async def result(self, job_id):
        return self._result or JobResult(id=job_id, backend=self.name, state=JobState.SUCCEEDED)

    async def cancel(self, job_id):
        return None


def test_candidates_filter_by_accelerator():
    router = BackendRouter(
        [FakeBackend("colab", ["T4", "A100"]), FakeBackend("modal", ["T4", "L4", "H100"])]
    )
    h100 = router.candidates(JobSpec(code="x", accelerator=Accelerator.H100))
    assert [b.name for b in h100] == ["modal"]
    t4 = router.candidates(JobSpec(code="x", accelerator=Accelerator.T4))
    assert [b.name for b in t4] == ["colab", "modal"]


def test_prefer_reorders():
    router = BackendRouter([FakeBackend("colab", ["T4"]), FakeBackend("modal", ["T4"])])
    cands = router.candidates(JobSpec(code="x", accelerator=Accelerator.T4), prefer="modal")
    assert [b.name for b in cands] == ["modal", "colab"]


def test_select_raises_when_unsupported():
    router = BackendRouter([FakeBackend("colab", ["T4"])])
    with pytest.raises(ConfigurationError):
        router.select(JobSpec(code="x", accelerator=Accelerator.H100))


async def test_run_fails_over_on_infra_error():
    bad = FakeBackend("colab", ["T4"], raises=AllocationError("no quota"))
    good = FakeBackend(
        "modal", ["T4"], result=JobResult(id="m", backend="modal", state=JobState.SUCCEEDED)
    )
    router = BackendRouter([bad, good])
    result = await router.run(JobSpec(code="x", accelerator=Accelerator.T4))
    assert result.backend == "modal"
    assert bad.run_calls == 1 and good.run_calls == 1


async def test_run_does_not_retry_a_failed_job():
    # A job that RAN but whose code failed must NOT trigger failover.
    failed = JobResult(id="c", backend="colab", state=JobState.FAILED, error="user code error")
    colab = FakeBackend("colab", ["T4"], result=failed)
    modal = FakeBackend("modal", ["T4"])
    router = BackendRouter([colab, modal])
    result = await router.run(JobSpec(code="x", accelerator=Accelerator.T4))
    assert result.state is JobState.FAILED
    assert modal.run_calls == 0  # not retried elsewhere


async def test_run_no_fallback_propagates():
    bad = FakeBackend("colab", ["T4"], raises=AllocationError("no quota"))
    router = BackendRouter([bad, FakeBackend("modal", ["T4"])])
    with pytest.raises(AllocationError):
        await router.run(JobSpec(code="x", accelerator=Accelerator.T4), fallback=False)


async def test_run_all_fail_raises():
    router = BackendRouter(
        [
            FakeBackend("colab", ["T4"], raises=AllocationError("a")),
            FakeBackend("modal", ["T4"], raises=ColabctlError("b")),
        ]
    )
    with pytest.raises(ColabctlError):
        await router.run(JobSpec(code="x", accelerator=Accelerator.T4))


# --- cheapest-first cost routing (Phase 2a) ---------------------------------


def _priced_router():
    # Names match the static price table: A100 → colab $1.50, runpod $1.89, modal $2.50.
    return BackendRouter(
        [
            FakeBackend("modal", ["A100", "H100"]),
            FakeBackend("colab", ["A100"]),
            FakeBackend("runpod", ["A100", "H100"]),
        ]
    )


async def test_cost_ranked_orders_cheapest_first():
    router = _priced_router()
    ranked = await router.cost_ranked(JobSpec(code="x", accelerator=Accelerator.A100))
    assert [b.name for (b, _p) in ranked] == ["colab", "runpod", "modal"]


async def test_cost_ranked_cap_is_fail_closed():
    router = _priced_router()
    ranked = await router.cost_ranked(
        JobSpec(code="x", accelerator=Accelerator.A100), max_price_usd_hr=2.0
    )
    assert [b.name for (b, _p) in ranked] == ["colab", "runpod"]  # modal ($2.50) dropped


async def test_cost_ranked_spot_only_keeps_spot_backends():
    router = _priced_router()
    ranked = await router.cost_ranked(JobSpec(code="x", accelerator=Accelerator.A100), spot=True)
    # only runpod has a spot A100 in the static table
    assert [b.name for (b, _p) in ranked] == ["runpod"]


async def test_run_cheapest_uses_lowest_priced_backend_first():
    router = _priced_router()
    await router.run(JobSpec(code="x", accelerator=Accelerator.A100), cheapest=True)
    ran = {b.name: b.run_calls for b in router._backends.values()}
    assert ran["colab"] == 1 and ran["modal"] == 0 and ran["runpod"] == 0  # cheapest ran


async def test_run_with_cap_refuses_fail_closed_when_none_qualify():
    router = _priced_router()
    with pytest.raises(AllocationError, match="fail-closed budget cap"):
        await router.run(
            JobSpec(code="x", accelerator=Accelerator.A100), max_price_usd_hr=1.0
        )  # nothing ≤ $1.00/hr → refuse, don't pick a pricier one
    assert all(b.run_calls == 0 for b in router._backends.values())  # never launched
