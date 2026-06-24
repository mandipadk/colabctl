"""Price model + catalog: cheapest-first lookup, spot rates, fallback (Phase 2a)."""

from __future__ import annotations

from colabctl.cost import GpuPrice, PriceCatalog, PriceSource, StaticPriceSource
from colabctl.models import Accelerator


def test_gpu_price_rate_prefers_spot_when_available():
    p = GpuPrice(
        provider="runpod", accelerator=Accelerator.A100, price_usd_hr=1.89, spot_price_usd_hr=1.19
    )
    assert p.rate() == 1.89  # on-demand by default
    assert p.rate(spot=True) == 1.19  # spot when requested
    nospot = GpuPrice(provider="modal", accelerator=Accelerator.A100, price_usd_hr=2.50)
    assert nospot.rate(spot=True) == 2.50  # no spot tier → falls back to on-demand


async def test_catalog_cheapest_across_backends():
    cat = PriceCatalog()  # static table only
    cheapest = await cat.cheapest(Accelerator.A100)
    assert cheapest is not None
    assert cheapest.provider == "colab"  # $1.50 effective beats modal/vertex/runpod on-demand


async def test_catalog_cheapest_spot_only():
    cat = PriceCatalog()
    cheapest = await cat.cheapest(Accelerator.A100, spot=True)
    assert cheapest is not None
    assert cheapest.provider == "runpod"  # only runpod has a spot A100 ($1.19)
    assert cheapest.rate(spot=True) == 1.19


async def test_catalog_cheapest_respects_backend_filter_and_cap():
    cat = PriceCatalog()
    # restrict to paid clouds → colab excluded
    paid = await cat.cheapest(Accelerator.A100, backends=["modal", "vertex", "runpod"])
    assert paid is not None and paid.provider == "runpod"  # $1.89 on-demand
    # a cap below every option → fail-closed (None)
    assert await cat.cheapest(Accelerator.A100, backends=["modal"], max_usd_hr=1.0) is None


async def test_catalog_per_backend_estimate_is_sorted():
    cat = PriceCatalog()
    rows = await cat.per_backend(Accelerator.A100)
    rates = [r.rate() for r in rows]
    assert rates == sorted(rates)  # cheapest first
    assert {r.provider for r in rows} >= {"colab", "modal", "vertex", "runpod"}


class _BoomSource(PriceSource):
    name = "boom"

    async def prices(self, *, accelerator=None):
        raise RuntimeError("feed down")


async def test_catalog_degrades_to_static_when_a_source_fails():
    # A flaky live source must never break routing — the static fallback answers.
    cat = PriceCatalog(sources=[_BoomSource()])
    cheapest = await cat.cheapest(Accelerator.H100)
    assert cheapest is not None  # came from the always-appended static source


async def test_catalog_prefers_an_earlier_live_source():
    cheap_h100 = [
        GpuPrice(provider="bargain", accelerator=Accelerator.H100, price_usd_hr=0.50, source="live")
    ]

    class _Live(PriceSource):
        name = "live"

        async def prices(self, *, accelerator=None):
            return [p for p in cheap_h100 if accelerator is None or p.accelerator == accelerator]

    cat = PriceCatalog(sources=[_Live()])
    cheapest = await cat.cheapest(Accelerator.H100)
    assert cheapest is not None and cheapest.provider == "bargain"  # live source wins over static


def test_static_source_filters_by_accelerator():
    import asyncio

    src = StaticPriceSource()
    rows = asyncio.run(src.prices(accelerator=Accelerator.H100))
    assert rows and all(r.accelerator is Accelerator.H100 for r in rows)
    assert {r.provider for r in rows} == {"modal", "vertex", "runpod"}  # colab/kaggle have no H100
