"""Cost engine: price discovery + cheapest-first routing inputs (Phase 2).

A backend-neutral price model (``GpuPrice``) behind a ``PriceSource`` chain with a
``PriceCatalog`` facade. Phase 2a ships the offline static table; Phase 2b swaps in live
feeds (SkyPilot catalog, ComputePrices) behind the same interface; the AWS spot-advisor
risk feed lands as a sibling source. All consumers (router, AllocationGate budget, the
``--dry-run`` estimator) depend only on this interface, never on a feed.
"""

from __future__ import annotations

from pathlib import Path

from colabctl.cost.price import (
    STATIC_GPU_PRICES,
    GpuPrice,
    PriceCatalog,
    PriceSource,
    StaticPriceSource,
)


def default_catalog(*, live: bool = False, home: Path | None = None) -> PriceCatalog:
    """The price catalog colabctl uses.

    ``live`` prepends the ComputePrices market feed (cached, plausibility-guarded) for fresh
    prices; the in-repo static table is always the trusted fallback. Routing and the budget cap
    default to ``live=False`` (the static floor is deterministic and offline-safe, and immune to
    aggregator unit-errors); ``colabctl cost --live`` opts into the live market view.
    """
    sources: list[PriceSource] = []
    if live:
        from colabctl.cost.feeds import ComputePricesSource

        sources.append(ComputePricesSource(home=home))
    return PriceCatalog(sources)


__all__ = [
    "STATIC_GPU_PRICES",
    "GpuPrice",
    "PriceCatalog",
    "PriceSource",
    "StaticPriceSource",
    "default_catalog",
]
