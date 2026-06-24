"""Live price-feed parsers (Phase 2b) — contract-tested hermetically against captured shapes.

The ``fetch`` is injected, so these never touch the network. A drift in the upstream schema
(renamed/removed fields) makes these fail loudly rather than silently misprice.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from colabctl.cost.feeds import ComputePricesSource
from colabctl.models import Accelerator

# A faithful slice of the real computeprices.com/api/v1/gpu-prices shape (June 2026), including
# an on-demand+spot pair to merge, a unit-error row to drop, and an unmodelled GPU to skip.
_SAMPLE = json.dumps(
    {
        "data": [
            {
                "provider": "RunPod",
                "gpu": "A100 80GB",
                "price_per_hour_usd": 1.39,
                "pricing_type": "on_demand",
                "last_updated": "2026-06-21T00:00:00Z",
            },
            {
                "provider": "RunPod",
                "gpu": "A100 80GB",
                "price_per_hour_usd": 0.99,
                "pricing_type": "spot",
                "last_updated": "2026-06-21T00:00:00Z",
            },
            {  # Modal H100 at $0.066/hr — a per-minute figure mislabeled hourly → must be dropped
                "provider": "Modal",
                "gpu": "H100 SXM",
                "price_per_hour_usd": 0.066,
                "pricing_type": "on_demand",
            },
            {  # RTX 4090 is not an accelerator colabctl models → skipped
                "provider": "Lambda",
                "gpu": "RTX 4090",
                "price_per_hour_usd": 0.50,
                "pricing_type": "on_demand",
            },
            {
                "provider": "Vast.ai",
                "gpu": "H100",
                "price_per_hour_usd": 1.99,
                "pricing_type": "on_demand",
            },
        ]
    }
).encode()


async def test_parses_merges_spot_and_drops_unit_errors(tmp_path: Path) -> None:
    calls: list[str] = []

    async def fetch(url: str) -> bytes:
        calls.append(url)
        return _SAMPLE

    rows = await ComputePricesSource(fetch=fetch, home=tmp_path).prices()
    by = {(r.provider, r.accelerator): r for r in rows}

    # on-demand + spot for the same (provider, accel) merge into one row
    rp = by[("runpod", Accelerator.A100)]
    assert rp.price_usd_hr == 1.39 and rp.spot_price_usd_hr == 0.99
    assert rp.source == "computeprices" and rp.last_updated is not None
    # provider slug normalization (Vast.ai → vast)
    assert ("vast", Accelerator.H100) in by
    # the unit-error Modal row is dropped; the unmodelled RTX 4090 is skipped
    assert {p for (p, _a) in by} == {"runpod", "vast"}
    assert len(calls) == 1


async def test_filter_by_accelerator(tmp_path: Path) -> None:
    async def fetch(url: str) -> bytes:
        return _SAMPLE

    rows = await ComputePricesSource(fetch=fetch, home=tmp_path).prices(
        accelerator=Accelerator.H100
    )
    assert rows and all(r.accelerator is Accelerator.H100 for r in rows)


async def test_caches_within_ttl(tmp_path: Path) -> None:
    calls: list[str] = []

    async def fetch(url: str) -> bytes:
        calls.append(url)
        return _SAMPLE

    src = ComputePricesSource(fetch=fetch, home=tmp_path)
    await src.prices()
    await src.prices()
    assert len(calls) == 1  # the second call is served from the on-disk cache


async def test_falls_back_to_last_good_on_fetch_error(tmp_path: Path) -> None:
    state = {"ok": True}

    async def fetch(url: str) -> bytes:
        if not state["ok"]:
            raise RuntimeError("network down")
        return _SAMPLE

    # ttl=0 forces a re-fetch every call, so the second call exercises the failure path
    src = ComputePricesSource(fetch=fetch, home=tmp_path, ttl=timedelta(0))
    first = await src.prices()
    state["ok"] = False
    second = await src.prices()  # fetch fails → last-good cache, not a crash
    assert {(r.provider, r.accelerator) for r in second} == {
        (r.provider, r.accelerator) for r in first
    }


async def test_default_catalog_static_is_offline_deterministic() -> None:
    from colabctl.cost import default_catalog

    cheapest = await default_catalog(live=False).cheapest(Accelerator.A100)
    assert cheapest is not None
    assert cheapest.provider == "colab" and cheapest.source == "static"


def test_default_catalog_live_prepends_market_feed() -> None:
    from colabctl.cost import default_catalog
    from colabctl.cost.feeds import ComputePricesSource

    cat = default_catalog(live=True)
    assert any(isinstance(s, ComputePricesSource) for s in cat._sources)  # live feed in the chain
