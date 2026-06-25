"""``colabctl doctor`` — preflight health checks (Phase 4.10.3).

Answers "why can't I get a GPU / why won't this run" *before* you burn time, by surfacing the
signals colabctl already has: ADC credentials, the default transport's ``colab`` binary,
which backends are configured, state-store health, and whether the agent skill is installed.

Checks are **offline and best-effort** by design — a missing credential is a ``warn`` with a
fix, never a crash — so ``doctor`` is safe to run anywhere and is fully unit-testable.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from colabctl.state import default_home


@dataclass
class Check:
    """One health check result. ``status`` is ``ok`` | ``warn`` | ``fail``."""

    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


#: Paid backends and the env var / credential file that indicates they're configured.
_BACKEND_HINTS: dict[str, tuple[str, Path | None]] = {
    "modal": ("MODAL_TOKEN_ID", Path.home() / ".modal.toml"),
    "vertex": ("GOOGLE_CLOUD_PROJECT", None),
    "runpod": ("RUNPOD_API_KEY", None),
    "vast": ("VAST_API_KEY", None),
    "hf": ("HF_TOKEN", None),
    "kaggle": ("KAGGLE_KEY", Path.home() / ".kaggle" / "kaggle.json"),
}


def _adc_check() -> Check:
    adc = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or str(
        Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    )
    if Path(adc).exists():
        return Check("auth-adc", "ok", f"ADC credentials present ({adc})")
    return Check(
        "auth-adc", "warn", "no ADC credentials found — run `colabctl auth login` for Colab/Drive"
    )


def _colab_binary_check() -> Check:
    found = shutil.which("colab")
    if found is None:
        sibling = Path(sys.executable).parent / "colab"
        found = str(sibling) if sibling.exists() else None
    if found is not None:
        return Check("colab-binary", "ok", found)
    return Check(
        "colab-binary",
        "warn",
        "google-colab-cli not found — the default `cli` transport can't run. Install with "
        '`uv tool install --with google-colab-cli "colabctl[all]"`, or use -t native / -t browser.',
    )


def _backends_check() -> Check:
    configured = [
        name
        for name, (env, path) in _BACKEND_HINTS.items()
        if os.environ.get(env) or (path is not None and path.exists())
    ]
    listed = ", ".join(["colab", *configured]) if configured else "colab (others not configured)"
    return Check("backends", "ok", f"configured: {listed}")


def _state_store_check(home: Path) -> Check:
    corrupt = list(home.glob("state.json.corrupt-*")) if home.exists() else []
    if corrupt:
        return Check(
            "state-store",
            "warn",
            f"{len(corrupt)} quarantined corrupt document(s) in {home} — run `colabctl job gc`",
        )
    return Check("state-store", "ok", str(home))


def _skill_check() -> Check:
    try:
        from colabctl import skills

        version = skills.installed_version("user")
    except Exception:  # pragma: no cover - defensive
        version = None
    if version:
        return Check("agent-skill", "ok", f"installed v{version}")
    return Check(
        "agent-skill",
        "warn",
        "not installed — `colabctl skill install` lets AI agents (Claude Code) discover colabctl.",
    )


def run_checks(*, home: Path | None = None) -> list[Check]:
    """Run all preflight checks and return their results (offline, never raises)."""
    h = home or default_home()
    return [
        _adc_check(),
        _colab_binary_check(),
        _backends_check(),
        _state_store_check(h),
        _skill_check(),
    ]


def overall_status(checks: list[Check]) -> str:
    """The worst status across ``checks`` (``fail`` > ``warn`` > ``ok``)."""
    statuses = {c.status for c in checks}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "ok"


__all__ = ["Check", "overall_status", "run_checks"]
