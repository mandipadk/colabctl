"""Spot interruption-risk reference, fed by AWS's free public Spot Advisor feed (Phase 2c).

The feed (``spot-advisor-data.json``) is the only authoritative, machine-readable per-GPU
interruption-rate + savings source. It is AWS-EC2-specific, so for colabctl it is a
**directional per-accelerator reference** — "A100 spot runs ~15-20% interruption, ~55%
savings" — not a per-backend (RunPod/Vast) guarantee. It gates the spot tier: prefer the
highest savings among accelerators whose interruption bucket is at/under a chosen ceiling.

httpx-only, lazy, cached on disk (the feed refreshes ~daily); the fetch is injectable so the
parser is contract-tested hermetically.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from colabctl.models import Accelerator
from colabctl.state import default_home, utcnow

Fetch = Callable[[str], Awaitable[bytes]]

#: AWS GPU instance family → the accelerator colabctl models. Families not mapped (g5/A10G,
#: p3/V100, g6e/L40S) are skipped — we only surface risk for accelerators we can route to.
_FAMILY_ACCEL: dict[str, Accelerator] = {
    "g4dn": Accelerator.T4,
    "g6": Accelerator.L4,
    "p4d": Accelerator.A100,
    "p4de": Accelerator.A100,
    "p5": Accelerator.H100,
}


def _family_accel(instance_type: str) -> Accelerator | None:
    family = instance_type.split(".", 1)[0]
    return _FAMILY_ACCEL.get(family)


async def _httpx_get(url: str) -> bytes:
    import httpx

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()
        return resp.content


class SpotRisk(BaseModel):
    """An aggregated spot-interruption reference for one accelerator (AWS EC2, directional)."""

    accelerator: Accelerator
    interruption_range: int  # 0-4 bucket index (0 = <5%, 4 = >20%)
    range_label: str  # human label resolved from the feed's own `ranges`
    savings_pct: int  # mean savings vs on-demand across sampled regions/types
    samples: int  # how many (region, instance_type) cells fed this aggregate

    def acceptable(self, max_range: int) -> bool:
        """Whether this accelerator's interruption bucket is at/under ``max_range`` (0-4)."""
        return self.interruption_range <= max_range


class _RiskCache:
    """On-disk last-good cache of parsed :class:`SpotRisk` rows."""

    def __init__(self, name: str, *, home: Path | None = None, ttl: timedelta) -> None:
        self._path = (home or default_home()) / "price-cache" / f"{name}.json"
        self._ttl = ttl

    def _read(self) -> tuple[datetime, list[SpotRisk]] | None:
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
            ts = datetime.fromisoformat(doc["fetched_at"])
            rows = [SpotRisk.model_validate(r) for r in doc["rows"]]
        except (OSError, ValueError, KeyError):
            return None
        return ts, rows

    def fresh(self) -> list[SpotRisk] | None:
        got = self._read()
        if got is not None and (utcnow() - got[0]) < self._ttl:
            return got[1]
        return None

    def last_good(self) -> list[SpotRisk]:
        got = self._read()
        return got[1] if got is not None else []

    def write(self, rows: list[SpotRisk]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        doc = {
            "fetched_at": utcnow().isoformat(),
            "rows": [r.model_dump(mode="json") for r in rows],
        }
        self._path.write_text(json.dumps(doc), encoding="utf-8")


class SpotRiskSource:
    """Per-accelerator spot interruption/savings, aggregated from the AWS Spot Advisor feed."""

    name = "aws-spot-advisor"
    URL = "https://spot-bid-advisor.s3.amazonaws.com/spot-advisor-data.json"

    def __init__(
        self,
        *,
        fetch: Fetch | None = None,
        home: Path | None = None,
        ttl: timedelta = timedelta(hours=24),
        os_name: str = "Linux",
    ) -> None:
        self._fetch = fetch or _httpx_get
        self._cache = _RiskCache(self.name, home=home, ttl=ttl)
        self._os = os_name

    async def risk(self, accelerator: Accelerator | None = None) -> list[SpotRisk]:
        rows = self._cache.fresh()
        if rows is None:
            try:
                rows = self._parse(await self._fetch(self.URL))
                self._cache.write(rows)
            except Exception:
                rows = self._cache.last_good()
        if accelerator is not None:
            rows = [r for r in rows if r.accelerator == accelerator]
        return rows

    def _parse(self, raw: bytes) -> list[SpotRisk]:
        doc = json.loads(raw)
        # Resolve bucket labels from the feed's own `ranges` (AWS may re-bucket; don't hardcode).
        labels = {int(r["index"]): str(r["label"]) for r in doc.get("ranges", [])}
        samples: dict[Accelerator, list[tuple[int, int]]] = {}
        for _region, by_os in doc.get("spot_advisor", {}).items():
            for itype, cell in by_os.get(self._os, {}).items():
                accel = _family_accel(itype)
                if accel is None:
                    continue
                try:
                    samples.setdefault(accel, []).append((int(cell["r"]), int(cell["s"])))
                except (KeyError, ValueError, TypeError):
                    continue
        out: list[SpotRisk] = []
        for accel, cells in samples.items():
            ranges = [r for r, _s in cells]
            savings = [s for _r, s in cells]
            # Representative bucket = median interruption across sampled cells (robust to outliers).
            rep_range = round(statistics.median(ranges))
            out.append(
                SpotRisk(
                    accelerator=accel,
                    interruption_range=rep_range,
                    range_label=labels.get(rep_range, f"bucket {rep_range}"),
                    savings_pct=round(statistics.mean(savings)),
                    samples=len(cells),
                )
            )
        return sorted(out, key=lambda r: r.interruption_range)


__all__ = ["SpotRisk", "SpotRiskSource"]
