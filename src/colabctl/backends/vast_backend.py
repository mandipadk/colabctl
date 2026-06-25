"""Vast.ai backend — bid-priced (interruptible/spot) and on-demand GPU rentals.

Vast is a peer marketplace: you search host *offers* and rent one. A **spot** instance is
launched by supplying a bid ``price``; an offer's ``min_bid`` is the current floor. There is
**no preemption warning** — when you're outbid the instance just leaves ``running`` while its
``intended_status`` stays ``running``, so preemption is detected by polling that tuple. Like
the RunPod backend this is IaaS: stdout is NOT captured (persist outputs to a volume / object
storage), and you must checkpoint frequently because a preempted host may be gone on relaunch.

Auth: ``Authorization: Bearer $VAST_API_KEY``. Talks raw ``/api/v0`` over httpx (no SDK
dependency); the request layer is injectable so orchestration is fake-tested. GPU names + the
search query are best-effort against the live API (not live-validated here — it spends real
money on real hosts); override ``min_reliability`` / pass your own bid as needed.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from colabctl.backends.base import (
    Backend,
    BackendCapabilities,
    JobInfo,
    JobResult,
    JobSpec,
    JobState,
)
from colabctl.errors import ColabctlError, ConfigurationError
from colabctl.models import Accelerator

#: A request layer — ``(method, path, json_body) -> response dict``. Injectable for tests.
Request = Callable[[str, str, dict[str, Any] | None], Awaitable[dict[str, Any]]]

# Accelerator → Vast `gpu_name` (underscored display names; best-effort, PCIe variants).
_VAST_GPU: dict[Accelerator, str] = {
    Accelerator.T4: "Tesla_T4",
    Accelerator.L4: "L4",
    Accelerator.A100: "A100_PCIE",
    Accelerator.H100: "H100_PCIE",
}
_DEFAULT_IMAGE = "pytorch/pytorch:2.1.0-cuda11.8-cudnn8-runtime"
_API_BASE = "https://console.vast.ai"
_POLL_INTERVAL = 15.0


def vast_gpu(accelerator: Accelerator) -> str:
    """Map our accelerator to a Vast ``gpu_name`` (GPU-only backend)."""
    if accelerator is Accelerator.NONE:
        raise ConfigurationError("Vast backend is GPU-only; pick a GPU accelerator.")
    if accelerator in _VAST_GPU:
        return _VAST_GPU[accelerator]
    raise ConfigurationError(f"Vast backend has no mapping for {accelerator.value!r}.")


def vast_state(instance: dict[str, Any]) -> JobState:
    """Map a Vast instance record to our :class:`JobState`, detecting bid preemption.

    The marketplace has no preemption signal, so a **bid** instance that has left ``running``
    while it is still ``intended`` to run was outbid → treat it as ``FAILED`` so recovery can
    re-bid / fail over. A clean ``exited``/``stopped`` (not a preemption) is ``SUCCEEDED``.
    """
    actual = str(instance.get("actual_status") or "").lower()
    intended = str(instance.get("intended_status") or "").lower()
    is_bid = bool(instance.get("is_bid"))
    if actual in ("running", "online"):
        return JobState.RUNNING
    if is_bid and intended == "running" and actual not in ("running", "online", ""):
        return JobState.FAILED  # preempted (outbid) — no warning is given
    if actual in ("exited", "stopped"):
        return JobState.SUCCEEDED
    if not actual:
        return JobState.PENDING
    return JobState.RUNNING


def _build_script(spec: JobSpec) -> str:
    code = spec.resolved_code()
    if spec.requirements:
        reqs = " ".join(shlex.quote(r) for r in spec.requirements)
        return f"pip install -q {reqs} && python -c {shlex.quote(code)}"
    return f"python -c {shlex.quote(code)}"


@dataclass
class _Job:
    info: JobInfo
    spot: bool = False


class VastBackend(Backend):
    """Run code on a Vast.ai GPU rental (bid/spot or on-demand)."""

    name = "vast"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        image: str = _DEFAULT_IMAGE,
        disk_gb: int = 20,
        min_reliability: float = 0.95,
        bid_per_gpu: float | None = None,
        poll_interval: float = _POLL_INTERVAL,
        request: Request | None = None,
    ) -> None:
        self._api_key = api_key
        self._image = image
        self._disk_gb = disk_gb
        self._min_reliability = min_reliability
        self._bid_per_gpu = bid_per_gpu
        self._poll_interval = poll_interval
        self._request = request or self._default_request
        self._jobs: dict[str, _Job] = {}

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            name=self.name,
            accelerators=["T4", "L4", "A100", "H100"],
            interactive=False,
            streaming_logs=False,
            persistent=False,
            requires_account=True,
            tos_posture="sanctioned",
            supports_spot=True,
            prepaid_wallet=True,
            preempt_notice_seconds=0,  # NONE — an outbid instance just stops; poll to detect
            notes=[
                "Peer marketplace — rents an independent host; stdout is NOT captured (persist "
                "to a volume / object storage). Hosts are filtered by reliability2.",
                "Spot (--spot) is bid-priced: needs a max bid (--max-price). NO preemption "
                "warning — checkpoint frequently; an outbid host may be gone on relaunch.",
                "Per-second billing, prepaid wallet — an empty balance blocks/loses rentals. "
                "Requires VAST_API_KEY.",
            ],
        )

    async def submit(self, spec: JobSpec) -> JobInfo:
        gpu_name = vast_gpu(spec.accelerator)
        offer = await self._cheapest_offer(gpu_name, spot=spec.spot)
        body: dict[str, Any] = {
            "client_id": "me",
            "image": self._image,
            "disk": self._disk_gb,
            "onstart": _build_script(spec),
            "env": spec.env or {},
        }
        if spec.spot:
            bid = self._bid_per_gpu if self._bid_per_gpu is not None else spec.max_price_usd_hr
            if bid is None:
                raise ConfigurationError(
                    "Vast spot needs a max bid per GPU-hour: set --max-price (or bid_per_gpu)."
                )
            body["price"] = bid  # presence of `price` ⇒ interruptible/bid instance
        resp = await self._request("PUT", f"/api/v0/asks/{offer['id']}/", body)
        contract = resp.get("new_contract")
        if not resp.get("success", True) or not contract:
            raise ColabctlError(f"Vast rental for offer {offer['id']} was not accepted: {resp}")
        info = JobInfo(
            id=str(contract),
            backend=self.name,
            state=JobState.RUNNING,
            accelerator=spec.accelerator,
            detail=(
                f"{'spot' if spec.spot else 'on-demand'} on host "
                f"{offer.get('geolocation', '?')}; https://cloud.vast.ai/instances/"
            ),
        )
        self._jobs[info.id] = _Job(info=info, spot=spec.spot)
        return info

    async def _cheapest_offer(self, gpu_name: str, *, spot: bool) -> dict[str, Any]:
        """Search bundles and return the cheapest rentable, reliable-enough offer."""
        query = {
            "q": {
                "gpu_name": {"eq": gpu_name},
                "num_gpus": {"gte": 1},
                "rentable": {"eq": True},
                "reliability2": {"gte": self._min_reliability},
                "type": "bid" if spot else "on_demand",
            }
        }
        resp = await self._request("POST", "/api/v0/bundles/", query)
        offers = [o for o in resp.get("offers", []) if o.get("id") is not None]
        if not offers:
            raise ColabctlError(
                f"Vast has no rentable {gpu_name} offer at reliability ≥ {self._min_reliability}."
            )
        key = "min_bid" if spot else "dph_total"
        return min(offers, key=lambda o: float(o.get(key) or o.get("dph_total") or 1e9))

    async def status(self, job_id: str) -> JobInfo:
        job = self._require(job_id)
        resp = await self._request("GET", f"/api/v0/instances/{job_id}/", None)
        instance = resp.get("instances") or resp.get("instance") or resp
        job.info.state = vast_state(instance if isinstance(instance, dict) else {})
        return job.info

    async def logs(self, job_id: str) -> str:
        return (
            "Vast does not expose instance stdout via the API. View it at "
            "https://cloud.vast.ai/instances/ (or write outputs to a mounted volume)."
        )

    async def result(self, job_id: str) -> JobResult:
        info = self._require(job_id).info
        try:
            while True:
                info = await self.status(job_id)
                if info.state.is_terminal:
                    break
                await asyncio.sleep(self._poll_interval)
        finally:
            await self._destroy(job_id)  # never leave a host billing
        preempted = self._require(job_id).spot and info.state is JobState.FAILED
        return JobResult(
            id=job_id,
            backend=self.name,
            state=info.state,
            stdout="",  # not captured — see logs() / use a volume
            error=(
                "spot instance preempted (outbid) — re-bid or fall back to on-demand"
                if preempted
                else (None if info.state is JobState.SUCCEEDED else "see Vast console / volume")
            ),
        )

    async def cancel(self, job_id: str) -> None:
        await self._destroy(job_id)
        self._require(job_id).info.state = JobState.CANCELLED

    async def _destroy(self, job_id: str) -> None:
        await self._request("DELETE", f"/api/v0/instances/{job_id}/", None)

    async def _default_request(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> dict[str, Any]:  # pragma: no cover - exercised only against the live API
        import httpx

        key = self._api_key or os.environ.get("VAST_API_KEY")
        if not key:
            raise ColabctlError("Vast backend needs VAST_API_KEY.")
        async with httpx.AsyncClient(timeout=60.0, base_url=_API_BASE) as client:
            resp = await client.request(
                method, path, json=body, headers={"Authorization": f"Bearer {key}"}
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
        return data

    def _require(self, job_id: str) -> _Job:
        job = self._jobs.get(job_id)
        if job is None:
            job = _Job(info=JobInfo(id=job_id, backend=self.name, state=JobState.UNKNOWN))
            self._jobs[job_id] = job
        return job
