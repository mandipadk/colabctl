"""Live GPU price feeds behind the :class:`~colabctl.cost.price.PriceSource` interface (2b).

httpx-only and lazy, each with an on-disk last-good cache, so a feed outage degrades to
``cached → static`` and never breaks routing. The network ``fetch`` is injectable, so the
parsers are tested hermetically against captured samples (a contract test fails loudly if the
upstream schema drifts).

Sources (verified June 2026):

* :class:`ComputePricesSource` — ``computeprices.com/api/v1/gpu-prices`` (free, no-auth public
  tier; rows carry ``pricing_type`` on_demand/spot/reserved). List prices for ranking, not
  binding quotes — always re-confirm at provisioning time.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from colabctl.cost.price import GpuPrice, PriceSource
from colabctl.models import Accelerator
from colabctl.state import default_home, utcnow

#: An async ``url -> bytes`` fetcher. Injectable so feed parsers are tested without network.
Fetch = Callable[[str], Awaitable[bytes]]


async def _httpx_get(url: str) -> bytes:
    import httpx

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()
        return resp.content


def accelerator_from_name(name: str) -> Accelerator | None:
    """Map a feed's free-text GPU name to our enum (None if it's not one we model)."""
    upper = name.upper()
    # Order matters: match the longest/most-specific token first.
    for accel in (
        Accelerator.H100,
        Accelerator.A100,
        Accelerator.L4,
        Accelerator.T4,
        Accelerator.G4,
    ):
        if accel.value in upper:
            return accel
    return None


_PROVIDER_SLUGS = {
    "runpod": "runpod",
    "vast.ai": "vast",
    "vast": "vast",
    "vastai": "vast",
    "google cloud": "vertex",
    "gcp": "vertex",
}

#: Hard per-accelerator ``$/hr`` floors below which an aggregated price is almost certainly a
#: unit error (a per-minute/second figure mislabeled as hourly — observed live for Modal rows,
#: e.g. an "H100 at $0.066/hr"). Such rows are DROPPED so they can never make a backend look
#: absurdly cheap and misroute real spend. Generous enough to keep legitimate spot rates.
_PLAUSIBLE_MIN_USD_HR: dict[Accelerator, float] = {
    Accelerator.T4: 0.05,
    Accelerator.G4: 0.08,
    Accelerator.L4: 0.12,
    Accelerator.A100: 0.35,
    Accelerator.H100: 0.70,
}


def _implausible(accel: Accelerator, price: float) -> bool:
    floor = _PLAUSIBLE_MIN_USD_HR.get(accel)
    return floor is not None and price < floor


@dataclass
class _Merge:
    """Accumulator for one (provider, accelerator) across the feed's per-type rows."""

    on_demand: float | None = None
    spot: float | None = None
    ts: datetime | None = None


def provider_slug(name: str) -> str:
    """Normalize a feed's provider name to a colabctl backend slug where one exists."""
    key = name.lower().strip()
    if key in _PROVIDER_SLUGS:
        return _PROVIDER_SLUGS[key]
    return re.sub(r"[^a-z0-9]+", "-", key).strip("-") or "unknown"


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class _FeedCache:
    """A tiny on-disk last-good cache of parsed rows (``~/.colabctl/price-cache/<name>.json``)."""

    def __init__(self, name: str, *, home: Path | None = None, ttl: timedelta) -> None:
        self._path = (home or default_home()) / "price-cache" / f"{name}.json"
        self._ttl = ttl

    def _read(self) -> tuple[datetime, list[GpuPrice]] | None:
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
            ts = datetime.fromisoformat(doc["fetched_at"])
            rows = [GpuPrice.model_validate(r) for r in doc["rows"]]
        except (OSError, ValueError, KeyError):
            return None
        return ts, rows

    def fresh(self) -> list[GpuPrice] | None:
        """Cached rows if still within the TTL, else None (caller should re-fetch)."""
        got = self._read()
        if got is not None and (utcnow() - got[0]) < self._ttl:
            return got[1]
        return None

    def last_good(self) -> list[GpuPrice]:
        """Whatever is cached, even if stale (the fetch-failure fallback)."""
        got = self._read()
        return got[1] if got is not None else []

    def write(self, rows: list[GpuPrice]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        doc = {
            "fetched_at": utcnow().isoformat(),
            "rows": [r.model_dump(mode="json") for r in rows],
        }
        self._path.write_text(json.dumps(doc), encoding="utf-8")


class ComputePricesSource(PriceSource):
    """ComputePrices.com — a free, no-auth aggregated GPU price index (~70 providers).

    Merges the feed's per-``pricing_type`` rows into one on-demand+spot :class:`GpuPrice` per
    (provider, accelerator). Cached ≥ the TTL (the public tier is only 10 req/hr/IP); on any
    fetch/parse error it returns the last-good cache so routing never breaks.
    """

    name = "computeprices"
    URL = "https://computeprices.com/api/v1/gpu-prices"

    def __init__(
        self,
        *,
        fetch: Fetch | None = None,
        home: Path | None = None,
        ttl: timedelta = timedelta(hours=6),
    ) -> None:
        self._fetch = fetch or _httpx_get
        self._cache = _FeedCache(self.name, home=home, ttl=ttl)

    async def prices(self, *, accelerator: Accelerator | None = None) -> list[GpuPrice]:
        rows = self._cache.fresh()
        if rows is None:
            try:
                rows = self._parse(await self._fetch(self.URL))
                self._cache.write(rows)
            except Exception:
                rows = self._cache.last_good()  # stale-but-real beats nothing
        if accelerator is not None:
            rows = [r for r in rows if r.accelerator == accelerator]
        return rows

    def _parse(self, raw: bytes) -> list[GpuPrice]:
        doc = json.loads(raw)
        merged: dict[tuple[str, Accelerator], _Merge] = {}
        for rec in doc.get("data", []):
            accel = accelerator_from_name(str(rec.get("gpu", "")))
            price = rec.get("price_per_hour_usd")
            if accel is None or price is None:
                continue
            rate = float(price)
            if _implausible(accel, rate):
                continue  # drop unit-error rows (per-minute/second mislabeled as hourly)
            slot = merged.setdefault(
                (provider_slug(str(rec.get("provider", "?"))), accel), _Merge()
            )
            ptype = str(rec.get("pricing_type", "on_demand"))
            if ptype == "spot":
                slot.spot = rate if slot.spot is None else min(slot.spot, rate)
            elif ptype == "on_demand" or slot.on_demand is None:
                slot.on_demand = rate  # prefer on_demand; reserved is only a backstop
            slot.ts = _parse_dt(rec.get("last_updated")) or slot.ts
        out: list[GpuPrice] = []
        for (prov, accel), slot in merged.items():
            on_demand = slot.on_demand if slot.on_demand is not None else slot.spot
            if on_demand is None:
                continue
            out.append(
                GpuPrice(
                    provider=prov,
                    accelerator=accel,
                    price_usd_hr=on_demand,
                    spot_price_usd_hr=slot.spot,
                    source=self.name,
                    last_updated=slot.ts,
                )
            )
        return out


__all__ = [
    "ComputePricesSource",
    "Fetch",
    "accelerator_from_name",
    "provider_slug",
]
