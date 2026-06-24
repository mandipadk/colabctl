"""The CLI `job run --allow` path actually invokes the failover router (Phase 0.3).

Before this, ``build_router`` had zero non-test callers and the documented "a Colab
outage degrades to Modal/Vertex" never executed in any user path. These tests assert the
wiring is real: with ``--allow`` an infra failure on the preferred backend fails over to
the next; without it, the router is never built.
"""

from __future__ import annotations

from typer.testing import CliRunner

from colabctl import cli as cli_mod
from colabctl.backends.router import BackendRouter
from colabctl.errors import AllocationError
from conftest import FakeBackend

runner = CliRunner()


class _FailingBackend(FakeBackend):
    """A backend whose allocation always fails with an infra error (triggers failover)."""

    async def run(self, spec) -> object:
        raise AllocationError(f"{self.name}: simulated infra failure")


def test_job_run_allow_fails_over_to_next_backend(monkeypatch) -> None:
    built: dict[str, list[str]] = {}

    def fake_router(names: list[str], state: object) -> BackendRouter:
        built["names"] = names
        return BackendRouter(
            [_FailingBackend("colab", accels=["T4"]), FakeBackend("modal", accels=["T4"])],
            order=["colab", "modal"],
        )

    monkeypatch.setattr(cli_mod, "_make_router", fake_router)
    result = runner.invoke(
        cli_mod.app,
        ["job", "run", "-c", "print(1)", "--backend", "colab", "--allow", "colab,modal"],
    )
    assert result.exit_code == 0, result.output
    assert "[modal] SUCCEEDED" in result.output  # failover executed → ran on modal
    assert built["names"] == ["colab", "modal"]


def test_job_run_without_allow_skips_the_router(monkeypatch) -> None:
    def fake_router(names: list[str], state: object) -> BackendRouter:  # pragma: no cover
        raise AssertionError("router must not be built without --allow")

    monkeypatch.setattr(cli_mod, "_make_router", fake_router)
    monkeypatch.setattr(
        cli_mod, "_make_backend", lambda name, state: FakeBackend("colab", accels=["T4"])
    )
    result = runner.invoke(cli_mod.app, ["job", "run", "-c", "print(1)", "--backend", "colab"])
    assert result.exit_code == 0, result.output
    assert "[colab] SUCCEEDED" in result.output
