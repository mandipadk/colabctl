"""CLI wiring for the native-only commands: `gc` and `attach`."""

from __future__ import annotations

import asyncio
from pathlib import Path

from typer.testing import CliRunner

from colabctl import cli as cli_mod
from colabctl.models import RuntimeSpec
from colabctl.sdk.client import ColabClient
from colabctl.state import StateStore
from colabctl.transport.native.adapter import NativeColabTransport
from conftest import FakeTransport
from test_native_attach import FakeClient, FakeKernel

runner = CliRunner()


def _native(tmp_path: Path) -> tuple[ColabClient, FakeClient, NativeColabTransport]:
    fake = FakeClient()

    def factory(url: str, token: str) -> FakeKernel:
        return FakeKernel()

    transport = NativeColabTransport(
        client=fake,  # type: ignore[arg-type]
        kernel_factory=factory,  # type: ignore[arg-type]
        state=StateStore(home=tmp_path / "home"),
        secrets=None,
    )
    return ColabClient(transport=transport), fake, transport


def test_gc_rejects_non_native(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_mod, "_make_client", lambda state: ColabClient(transport=FakeTransport())
    )
    result = runner.invoke(cli_mod.app, ["gc"])
    assert result.exit_code == 1
    assert "native transport" in result.output


def test_attach_rejects_non_native(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_mod, "_make_client", lambda state: ColabClient(transport=FakeTransport())
    )
    result = runner.invoke(cli_mod.app, ["attach", "x"])
    assert result.exit_code == 1
    assert "native transport" in result.output


def test_gc_releases_orphan(monkeypatch, tmp_path) -> None:
    client, fake, _ = _native(tmp_path)
    fake.add_orphan("gpu-orphan")
    monkeypatch.setattr(cli_mod, "_make_client", lambda state: client)
    result = runner.invoke(cli_mod.app, ["gc", "--release-orphans"])
    assert result.exit_code == 0
    assert "gpu-orphan" in result.output
    assert "gpu-orphan" in fake.unassigned


def test_gc_default_reports_orphan_without_releasing(monkeypatch, tmp_path) -> None:
    client, fake, _ = _native(tmp_path)
    fake.add_orphan("gpu-orphan")
    monkeypatch.setattr(cli_mod, "_make_client", lambda state: client)
    result = runner.invoke(cli_mod.app, ["gc"])
    assert result.exit_code == 0
    assert "--release-orphans" in result.output  # hint shown
    assert fake.unassigned == []  # nothing released by default


def test_attach_prints_recovered_session(monkeypatch, tmp_path) -> None:
    client, _, transport = _native(tmp_path)
    asyncio.run(transport.allocate(RuntimeSpec(name="job1")))
    monkeypatch.setattr(cli_mod, "_make_client", lambda state: client)
    result = runner.invoke(cli_mod.app, ["attach", "job1"])
    assert result.exit_code == 0
    assert "job1" in result.output


def test_interrupt_command_delegates(monkeypatch) -> None:
    t = FakeTransport()
    monkeypatch.setattr(cli_mod, "_make_client", lambda state: ColabClient(transport=t))
    result = runner.invoke(cli_mod.app, ["interrupt", "j"])
    assert result.exit_code == 0
    assert t.interrupts == ["j"]
    assert "Interrupted j" in result.output


def test_quota_command_prints_ccu_info(monkeypatch) -> None:
    class _Ccu(FakeTransport):
        async def ccu_info(self):
            return {"computeUnits": 7}

    monkeypatch.setattr(cli_mod, "_make_client", lambda state: ColabClient(transport=_Ccu()))
    result = runner.invoke(cli_mod.app, ["quota"])
    assert result.exit_code == 0
    assert "computeUnits" in result.output


def test_quota_command_non_native(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_mod, "_make_client", lambda state: ColabClient(transport=FakeTransport())
    )
    result = runner.invoke(cli_mod.app, ["quota"])
    assert result.exit_code == 0
    assert "native transport" in result.output


def test_too_many_assignments_suggests_gc(monkeypatch) -> None:
    from colabctl.errors import TooManyAssignmentsError

    class _Boom(FakeTransport):
        async def list_sessions(self):
            raise TooManyAssignmentsError("412: too many assignments")

    monkeypatch.setattr(cli_mod, "_make_client", lambda state: ColabClient(transport=_Boom()))
    result = runner.invoke(cli_mod.app, ["sessions"])
    assert result.exit_code == 1
    assert "gc --release-orphans" in result.output
