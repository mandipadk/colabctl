"""Capability-based backend routing with failover.

The router picks a backend that supports the requested accelerator (honoring an
optional preference + a fixed order) and, on **infrastructure** failure, fails over
to the next candidate. A job that *ran* but whose user code failed is NOT retried
elsewhere — that's a code problem, not a backend problem; only raised
:class:`ColabctlError`s (allocation/quota/transport) trigger failover.
"""

from __future__ import annotations

from colabctl.backends.base import Backend, JobResult, JobSpec
from colabctl.errors import AllocationError, ColabctlError, ConfigurationError


class BackendRouter:
    """Selects among registered backends and fails over on infra errors."""

    def __init__(self, backends: list[Backend], *, order: list[str] | None = None) -> None:
        self._backends: dict[str, Backend] = {b.name: b for b in backends}
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

    async def run(
        self, spec: JobSpec, *, prefer: str | None = None, fallback: bool = True
    ) -> JobResult:
        """Run ``spec`` on the best backend, failing over on infra errors."""
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
