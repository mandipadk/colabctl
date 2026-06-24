"""Cost engine: price discovery + cheapest-first routing inputs (Phase 2).

A backend-neutral price model (``GpuPrice``) behind a ``PriceSource`` chain with a
``PriceCatalog`` facade. Phase 2a ships the offline static table; Phase 2b swaps in live
feeds (SkyPilot catalog, ComputePrices) behind the same interface; the AWS spot-advisor
risk feed lands as a sibling source. All consumers (router, AllocationGate budget, the
``--dry-run`` estimator) depend only on this interface, never on a feed.
"""

from __future__ import annotations

from colabctl.cost.price import (
    STATIC_GPU_PRICES,
    GpuPrice,
    PriceCatalog,
    PriceSource,
    StaticPriceSource,
)

__all__ = [
    "STATIC_GPU_PRICES",
    "GpuPrice",
    "PriceCatalog",
    "PriceSource",
    "StaticPriceSource",
]
