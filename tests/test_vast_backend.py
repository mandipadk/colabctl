"""Vast.ai backend: gpu/state mapping, bid search/launch, preemption — vs a fake /api/v0."""

from __future__ import annotations

from typing import Any

import pytest

from colabctl.backends.base import JobSpec, JobState
from colabctl.backends.vast_backend import VastBackend, vast_gpu, vast_state
from colabctl.errors import ColabctlError, ConfigurationError
from colabctl.models import Accelerator


def test_vast_gpu_mapping():
    assert vast_gpu(Accelerator.A100) == "A100_PCIE"
    assert vast_gpu(Accelerator.T4) == "Tesla_T4"
    with pytest.raises(ConfigurationError):
        vast_gpu(Accelerator.NONE)  # GPU-only
    with pytest.raises(ConfigurationError):
        vast_gpu(Accelerator.G4)


def test_vast_state_detects_bid_preemption():
    assert (
        vast_state({"actual_status": "running", "intended_status": "running"}) is JobState.RUNNING
    )
    # bid instance left running while still intended to run → preempted (outbid)
    preempted = {"actual_status": "exited", "intended_status": "running", "is_bid": True}
    assert vast_state(preempted) is JobState.FAILED
    # a clean exit (not intended to keep running) is success
    clean = {"actual_status": "exited", "intended_status": "exited", "is_bid": True}
    assert vast_state(clean) is JobState.SUCCEEDED
    assert vast_state({}) is JobState.PENDING


class FakeVast:
    def __init__(self, *, status=("exited", "exited"), accepted=True):
        self.offers = [
            {
                "id": 111,
                "min_bid": 0.50,
                "dph_total": 1.2,
                "reliability2": 0.98,
                "geolocation": "US",
            },
            {
                "id": 222,
                "min_bid": 0.30,
                "dph_total": 0.9,
                "reliability2": 0.97,
                "geolocation": "EU",
            },
        ]
        self._actual, self._intended = status
        self._accepted = accepted
        self.calls: list[tuple[str, str, dict | None]] = []

    async def __call__(self, method: str, path: str, body: dict[str, Any] | None) -> dict:
        self.calls.append((method, path, body))
        if path == "/api/v0/bundles/":
            return {"offers": self.offers}
        if path.startswith("/api/v0/asks/"):
            return {"success": self._accepted, "new_contract": 9001 if self._accepted else None}
        if path.startswith("/api/v0/instances/") and method == "GET":
            inst = {
                "actual_status": self._actual,
                "intended_status": self._intended,
                "is_bid": True,
            }
            return {"instances": inst}
        return {"success": True}  # DELETE


def test_vast_capabilities_advertise_no_warning_spot():
    caps = VastBackend().capabilities
    assert caps.supports_spot and caps.prepaid_wallet
    assert caps.preempt_notice_seconds == 0  # NO preemption warning


async def test_vast_spot_picks_cheapest_offer_and_bids():
    fake = FakeVast()
    backend = VastBackend(request=fake)
    info = await backend.submit(
        JobSpec(code="train()", accelerator=Accelerator.A100, spot=True, max_price_usd_hr=1.0)
    )
    assert info.state is JobState.RUNNING and info.id == "9001"
    # searched bundles (bid), then PUT the bid on the CHEAPEST offer (222, min_bid 0.30)
    methods = [(m, p) for (m, p, _b) in fake.calls]
    assert ("POST", "/api/v0/bundles/") in methods
    put = next(b for (m, p, b) in fake.calls if p == "/api/v0/asks/222/")
    assert put is not None and put["price"] == 1.0  # the bid (from --max-price)
    search_q = next(b for (m, p, b) in fake.calls if p == "/api/v0/bundles/")
    assert search_q["q"]["type"] == "bid" and search_q["q"]["gpu_name"]["eq"] == "A100_PCIE"


async def test_vast_spot_requires_a_bid():
    backend = VastBackend(request=FakeVast())
    with pytest.raises(ConfigurationError, match="max bid"):
        await backend.submit(JobSpec(code="x=1", accelerator=Accelerator.A100, spot=True))


async def test_vast_is_gpu_only():
    with pytest.raises(ConfigurationError):
        await VastBackend(request=FakeVast()).submit(
            JobSpec(code="x=1", accelerator=Accelerator.NONE)
        )


async def test_vast_result_reports_preemption_and_destroys():
    fake = FakeVast(status=("exited", "running"))  # preempted: left running, still intended
    backend = VastBackend(request=fake)
    info = await backend.submit(
        JobSpec(code="x=1", accelerator=Accelerator.A100, spot=True, max_price_usd_hr=1.0)
    )
    result = await backend.result(info.id)
    assert result.state is JobState.FAILED
    assert result.error is not None and "preempted" in result.error
    assert any(m == "DELETE" for (m, _p, _b) in fake.calls)  # host torn down (never left billing)


async def test_vast_rental_rejected_is_infra_error():
    backend = VastBackend(request=FakeVast(accepted=False))
    with pytest.raises(ColabctlError, match="not accepted"):
        await backend.submit(
            JobSpec(code="x=1", accelerator=Accelerator.H100, spot=True, max_price_usd_hr=2.0)
        )
