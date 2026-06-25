"""GPU price model + source chain + cheapest-price catalog.

``GpuPrice`` normalizes every backend/feed to a SkyPilot-catalog-shaped row (on-demand +
spot ``$/hr`` per accelerator), so prices are diffable and joinable. ``PriceSource`` is the
pluggable feed interface; ``PriceCatalog`` is the consumer facade that queries an ordered
source chain and always falls back to the in-repo static table — so cheapest-routing and the
USD cap **always** have a number to reason about, even with every live feed down.

Phase 2a ships only :class:`StaticPriceSource`; Phase 2b adds httpx-backed live sources
behind the same ABC, with the static table demoted to the fallback (never removed).
"""

from __future__ import annotations

import abc
from datetime import datetime

from pydantic import BaseModel

from colabctl.models import Accelerator


class GpuPrice(BaseModel):
    """One normalized price row: a backend's on-demand (and optional spot) ``$/hr``."""

    provider: str  # the colabctl backend name (e.g. "modal", "runpod", "colab")
    accelerator: Accelerator
    price_usd_hr: float  # on-demand USD/hour
    spot_price_usd_hr: float | None = None  # interruptible USD/hour (None = no spot tier)
    region: str | None = None
    source: str = "static"  # which feed emitted this row
    currency: str = "USD"
    last_updated: datetime | None = None

    def rate(self, *, spot: bool = False) -> float:
        """Effective ``$/hr``: the spot price when requested *and* available, else on-demand."""
        if spot and self.spot_price_usd_hr is not None:
            return self.spot_price_usd_hr
        return self.price_usd_hr


def _p(provider: str, accel: Accelerator, on_demand: float, spot: float | None = None) -> GpuPrice:
    return GpuPrice(
        provider=provider, accelerator=accel, price_usd_hr=on_demand, spot_price_usd_hr=spot
    )


#: Hand-maintained, conservative USD/hour estimates (June 2026), updated by PR. The live
#: feeds in Phase 2b supersede these; they exist so the cost engine has a zero-network floor
#: and so 2a is fully testable offline. Colab/Kaggle are modelled at their effective marginal
#: cost (Kaggle's free weekly quota = $0; Colab Pro's compute-unit burn ≈ a low $/hr).
STATIC_GPU_PRICES: list[GpuPrice] = [
    # Colab Pro — effective compute-unit cost (not a true $/hr; superseded by live quota math)
    _p("colab", Accelerator.T4, 0.18),
    _p("colab", Accelerator.L4, 0.35),
    _p("colab", Accelerator.A100, 1.50),
    # Kaggle — free weekly GPU quota
    _p("kaggle", Accelerator.T4, 0.0),
    # Modal — serverless per-second
    _p("modal", Accelerator.T4, 0.59),
    _p("modal", Accelerator.L4, 0.80),
    _p("modal", Accelerator.A100, 2.50),
    _p("modal", Accelerator.H100, 3.95),
    # Vertex AI — managed, premium
    _p("vertex", Accelerator.T4, 0.35),
    _p("vertex", Accelerator.L4, 0.71),
    _p("vertex", Accelerator.A100, 3.67),
    _p("vertex", Accelerator.H100, 11.0),
    # RunPod — on-demand + spot (community/interruptible)
    _p("runpod", Accelerator.T4, 0.39, 0.20),
    _p("runpod", Accelerator.L4, 0.43, 0.24),
    _p("runpod", Accelerator.A100, 1.89, 1.19),
    _p("runpod", Accelerator.H100, 4.18, 1.75),
    # Vast.ai — bid marketplace; on-demand + (cheaper) spot floors
    _p("vast", Accelerator.T4, 0.25, 0.12),
    _p("vast", Accelerator.L4, 0.45, 0.22),
    _p("vast", Accelerator.A100, 1.10, 0.67),
    _p("vast", Accelerator.H100, 2.40, 1.65),
]


class PriceSource(abc.ABC):
    """A pluggable GPU price feed. Implementations are lazy-imported and httpx-only."""

    name: str = "source"

    @abc.abstractmethod
    async def prices(self, *, accelerator: Accelerator | None = None) -> list[GpuPrice]:
        """Return price rows, optionally filtered to one accelerator."""

    async def aclose(self) -> None:
        return None


class StaticPriceSource(PriceSource):
    """The offline static price table (the always-present fallback / 2a's only source)."""

    name = "static"

    def __init__(self, table: list[GpuPrice] | None = None) -> None:
        self._table = table if table is not None else STATIC_GPU_PRICES

    async def prices(self, *, accelerator: Accelerator | None = None) -> list[GpuPrice]:
        return [p for p in self._table if accelerator is None or p.accelerator == accelerator]


class PriceCatalog:
    """Cheapest-price lookup over an ordered ``PriceSource`` chain + a static fallback."""

    def __init__(self, sources: list[PriceSource] | None = None) -> None:
        # The static source is always appended last so a feed outage degrades to it.
        self._sources = [*(sources or []), StaticPriceSource()]

    async def prices(self, *, accelerator: Accelerator | None = None) -> list[GpuPrice]:
        """Rows from the first source that returns any (then the static fallback)."""
        for source in self._sources:
            try:
                rows = await source.prices(accelerator=accelerator)
            except Exception:  # a flaky feed must never break routing
                continue
            if rows:
                return rows
        return []

    async def cheapest(
        self,
        accelerator: Accelerator,
        *,
        spot: bool = False,
        backends: list[str] | None = None,
        max_usd_hr: float | None = None,
    ) -> GpuPrice | None:
        """The lowest-rate eligible row (spot/on-demand), or None if nothing qualifies."""
        rows = await self.prices(accelerator=accelerator)
        cands = [p for p in rows if backends is None or p.provider in backends]
        if spot:
            cands = [p for p in cands if p.spot_price_usd_hr is not None]
        if max_usd_hr is not None:
            cands = [p for p in cands if p.rate(spot=spot) <= max_usd_hr]
        return min(cands, key=lambda p: p.rate(spot=spot)) if cands else None

    async def per_backend(
        self,
        accelerator: Accelerator,
        *,
        spot: bool = False,
        backends: list[str] | None = None,
    ) -> list[GpuPrice]:
        """The cheapest row per backend for ``accelerator``, sorted by rate — the dry-run view."""
        best: dict[str, GpuPrice] = {}
        for p in await self.prices(accelerator=accelerator):
            if backends is not None and p.provider not in backends:
                continue
            if spot and p.spot_price_usd_hr is None:
                continue
            cur = best.get(p.provider)
            if cur is None or p.rate(spot=spot) < cur.rate(spot=spot):
                best[p.provider] = p
        return sorted(best.values(), key=lambda p: p.rate(spot=spot))


__all__ = [
    "STATIC_GPU_PRICES",
    "GpuPrice",
    "PriceCatalog",
    "PriceSource",
    "StaticPriceSource",
]
