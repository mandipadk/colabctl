"""Pre-allocation spend guard for Colab compute units.

An autonomous agent loop must not be able to run an account's balance to zero by
accident — the directive's "hard spend caps" applied to Colab's currency. Using the
verified ``ccu-info`` shape (balance, hourly burn, entitled accelerators), this classifies
a requested allocation into **blockers** (refuse unless explicitly overridden) and
**warnings** (surface, but proceed). Pure and transport-agnostic; the CLI wires it in
front of native allocate, where ``ccu-info`` is available.
"""

from __future__ import annotations

from collections.abc import Iterable

from colabctl.models import Accelerator, CcuInfo


def spend_report(
    ccu: CcuInfo | None, accelerators: Iterable[Accelerator]
) -> tuple[list[str], list[str]]:
    """Return ``(blockers, warnings)`` for allocating one of ``accelerators`` given ``ccu``.

    Blocker: a non-positive compute-unit balance (allocation will fail / strand the user).
    Warnings: a short runway at the current burn rate, or none of the requested GPUs being
    in the entitled set (the allocation ladder / backend still arbitrates actual
    entitlement, so this is informational, not fatal).
    """
    blockers: list[str] = []
    warnings: list[str] = []
    if ccu is None:
        return blockers, warnings

    if ccu.current_balance is not None and ccu.current_balance <= 0:
        blockers.append(
            f"compute-unit balance is {ccu.current_balance:.2f} — allocation will likely fail"
        )
    if ccu.runway_hours is not None and ccu.runway_hours < 1.0:
        rate = ccu.consumption_rate_hourly or 0.0
        warnings.append(
            f"~{ccu.runway_hours:.1f}h of balance left at the current burn rate ({rate:.2f}/h)"
        )
    gpus = [a for a in accelerators if a.is_gpu]
    if gpus and ccu.eligible_gpus and not any(a.value in ccu.eligible_gpus for a in gpus):
        requested = ", ".join(a.value for a in gpus)
        warnings.append(
            f"none of [{requested}] are in your entitled GPUs ({', '.join(ccu.eligible_gpus)})"
        )
    return blockers, warnings


__all__ = ["spend_report"]
