"""Capability-based backend routing with failover.

The router picks a backend that supports the requested accelerator (honoring an
optional preference + a fixed order) and, on **infrastructure** failure, fails over
to the next candidate. A job that *ran* but whose user code failed is NOT retried
elsewhere — that's a code problem, not a backend problem; only raised
:class:`ColabctlError`s (allocation/quota/transport) trigger failover.
"""

from __future__ import annotations

from colabctl.backends.base import Backend, JobResult, JobSpec
from colabctl.cost import GpuPrice, PriceCatalog
from colabctl.errors import AllocationError, ColabctlError, ConfigurationError


class BackendRouter:
    """Selects among registered backends and fails over on infra errors."""

    def __init__(
        self,
        backends: list[Backend],
        *,
        order: list[str] | None = None,
        catalog: PriceCatalog | None = None,
    ) -> None:
        self._backends: dict[str, Backend] = {b.name: b for b in backends}
        self._catalog = catalog or PriceCatalog()
        if order is None:
            ordered = [b.name for b in backends]
        else:
            unknown = [n for n in order if n not in self._backends]
            if unknown:
                raise ConfigurationError(f"order references unregistered backend(s): {unknown}")
            # Honor the given order first, then append any registered backend it
            # omitted so nothing we registered is silently unreachable.
            ordered = [*order, *(n for n in self._backends if n not in order)]
        # Dedup while preserving first occurrence: duplicate names would otherwise
        # make failover re-run the same backend.
        seen: set[str] = set()
        self._order: list[str] = []
        for n in ordered:
            if n not in seen:
                seen.add(n)
                self._order.append(n)

    def register(self, backend: Backend) -> None:
        self._backends[backend.name] = backend
        if backend.name not in self._order:
            self._order.append(backend.name)

    def get(self, name: str) -> Backend:
        try:
            return self._backends[name]
        except KeyError as exc:
            raise ConfigurationError(f"No backend named {name!r} is registered.") from exc

    def candidates(self, spec: JobSpec, *, prefer: str | None = None) -> list[Backend]:
        """Ordered backends that support ``spec.accelerator`` (``prefer`` first)."""
        names = list(self._order)
        if prefer is not None:
            if prefer not in self._backends:
                raise ConfigurationError(f"Preferred backend {prefer!r} is not registered.")
            names = [prefer, *[n for n in names if n != prefer]]
        return [
            self._backends[n]
            for n in names
            if self._backends[n].capabilities.supports(spec.accelerator)
        ]

    def select(self, spec: JobSpec, *, prefer: str | None = None) -> Backend:
        cands = self.candidates(spec, prefer=prefer)
        if not cands:
            raise ConfigurationError(
                f"No registered backend supports accelerator {spec.accelerator.value!r}."
            )
        return cands[0]

    async def cost_ranked(
        self,
        spec: JobSpec,
        *,
        prefer: str | None = None,
        spot: bool = False,
        max_price_usd_hr: float | None = None,
    ) -> list[tuple[Backend, GpuPrice | None]]:
        """Capable backends priced and sorted cheapest-first.

        Each candidate is paired with its cheapest price row for ``spec.accelerator`` (None if
        the catalog has no price for it). When ``max_price_usd_hr`` is set the ordering is
        **fail-closed**: any backend whose rate is above the cap — or that has no price to
        check against the cap — is dropped, so an empty result means "refuse", never "pick a
        pricier one". Unpriced backends sort last when no cap is given.
        """
        ranked: list[tuple[Backend, GpuPrice | None, float | None]] = []
        for backend in self.candidates(spec, prefer=prefer):
            price = await self._catalog.cheapest(
                spec.accelerator, spot=spot, backends=[backend.name]
            )
            if spot and price is None:
                continue  # spot requested but this backend has no spot tier for the accelerator
            rate = price.rate(spot=spot) if price is not None else None
            if max_price_usd_hr is not None and (rate is None or rate > max_price_usd_hr):
                continue  # over the cap, or unpriced-under-a-cap → fail-closed exclude
            ranked.append((backend, price, rate))
        # Cheapest first; unpriced (rate None) sort last.
        ranked.sort(key=lambda t: (t[2] is None, t[2] or 0.0))
        return [(b, p) for (b, p, _r) in ranked]

    async def run(
        self,
        spec: JobSpec,
        *,
        prefer: str | None = None,
        fallback: bool = True,
        cheapest: bool = False,
        spot: bool = False,
        max_price_usd_hr: float | None = None,
    ) -> JobResult:
        """Run ``spec`` on the best backend, failing over on infra errors.

        With ``cheapest`` (or a ``max_price_usd_hr`` cap) candidates are ordered cheapest-first
        and filtered to those at-or-below the cap (fail-closed). Otherwise the fixed
        capability order applies, ``prefer`` first.
        """
        if cheapest or max_price_usd_hr is not None:
            ranked = await self.cost_ranked(
                spec, prefer=prefer, spot=spot, max_price_usd_hr=max_price_usd_hr
            )
            cands = [b for (b, _p) in ranked]
            if not cands:
                raise AllocationError(
                    f"No registered backend offers {spec.accelerator.value} at or below "
                    f"${max_price_usd_hr:.2f}/hr; refusing to launch (fail-closed budget cap)."
                    if max_price_usd_hr is not None
                    else f"No registered backend supports accelerator {spec.accelerator.value!r}."
                )
        else:
            cands = self.candidates(spec, prefer=prefer)
            if not cands:
                raise ConfigurationError(
                    f"No registered backend supports accelerator {spec.accelerator.value!r}."
                )
        errors: list[str] = []
        for backend in cands:
            try:
                return await backend.run(spec)
            except (AllocationError, ColabctlError) as exc:
                errors.append(f"{backend.name}: {exc}")
                if not fallback:
                    raise
        raise ColabctlError("All candidate backends failed:\n  " + "\n  ".join(errors))

    async def aclose(self) -> None:
        for backend in self._backends.values():
            await backend.aclose()
