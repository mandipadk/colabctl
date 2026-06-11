"""Tests for ColabClient + ColabSession against the FakeTransport."""

from __future__ import annotations

import pytest

from colabctl.errors import AcceleratorUnavailableError, ConfigurationError
from colabctl.models import Accelerator
from colabctl.sdk import ColabClient
from colabctl.sdk.client import _resolve_ladder
from conftest import FakeTransport


class _LadderTransport(FakeTransport):
    """Raises AcceleratorUnavailableError for a configured set of accelerators."""

    def __init__(self, *, unavailable: set[Accelerator]) -> None:
        super().__init__()
        self._unavailable = unavailable
        self.attempted: list[Accelerator] = []

    async def allocate(self, spec):
        self.attempted.append(spec.accelerator)
        if spec.accelerator in self._unavailable:
            raise AcceleratorUnavailableError("no quota", accelerator=spec.accelerator.value)
        return await super().allocate(spec)


async def test_allocate_returns_session_with_info():
    t = FakeTransport()
    client = ColabClient(transport=t)
    session = await client.allocate(gpu="A100", name="job1")
    assert session.name == "job1"
    assert session.info is not None
    assert session.info.accelerator is Accelerator.A100


async def test_default_accelerator_is_t4():
    t = FakeTransport()
    client = ColabClient(transport=t)
    session = await client.allocate(name="j")
    assert session.info is not None
    assert session.info.accelerator is Accelerator.T4


async def test_unknown_gpu_raises():
    client = ColabClient(transport=FakeTransport())
    with pytest.raises(ConfigurationError):
        await client.allocate(gpu="B200")


async def test_session_run_and_outputs():
    t = FakeTransport()
    client = ColabClient(transport=t)
    session = await client.allocate(name="j")
    result = await session.run("print('hi')")
    assert result.ok
    assert t.executed == [("j", "print('hi')")]
    assert "ran:" in result.text


async def test_context_manager_stops_when_owned():
    t = FakeTransport()
    async with ColabClient(transport=t) as client:
        async with await client.allocate(name="j") as session:
            await session.run("x=1")
    assert t.stopped == ["j"]
    assert t.closed is True


async def test_keep_true_does_not_stop():
    t = FakeTransport()
    client = ColabClient(transport=t)
    async with await client.allocate(name="j", keep=True):
        pass
    assert t.stopped == []


async def test_attach_does_not_own():
    t = FakeTransport()
    client = ColabClient(transport=t)
    await client.allocate(name="j", keep=True)
    async with client.attach("j"):
        pass
    assert t.stopped == []  # attach never auto-stops


async def test_upload_download_roundtrip(tmp_path):
    t = FakeTransport()
    client = ColabClient(transport=t)
    session = await client.allocate(name="j")
    local = tmp_path / "a.txt"
    local.write_text("hello")
    await session.upload(local, "content/a.txt")
    assert t.uploaded == [("j", str(local), "content/a.txt")]

    dest = tmp_path / "b.txt"
    await session.download("content/a.txt", dest)
    assert dest.read_text() == "downloaded"


async def test_keep_alive_via_native_only():
    t = FakeTransport()
    session = ColabClient(transport=t).attach("j")
    await session.keep_alive()
    assert t.keepalives == ["j"]


async def test_interrupt_delegates_to_transport():
    t = FakeTransport()
    session = ColabClient(transport=t).attach("j")
    await session.interrupt()
    assert t.interrupts == ["j"]


async def test_list_sessions():
    t = FakeTransport()
    client = ColabClient(transport=t)
    await client.allocate(name="j1", keep=True)
    await client.allocate(name="j2", keep=True)
    names = {s.name for s in await client.list_sessions()}
    assert names == {"j1", "j2"}


def test_unknown_transport_name_raises():
    with pytest.raises(ConfigurationError):
        ColabClient(transport_name="bogus")


# -- allocation ladder (§5.2) -------------------------------------------------


def test_resolve_ladder_parses_csv_and_dedups():
    assert _resolve_ladder("A100,L4,T4", None, Accelerator.T4) == [
        Accelerator.A100,
        Accelerator.L4,
        Accelerator.T4,
    ]
    assert _resolve_ladder("T4", None, Accelerator.T4) == [Accelerator.T4]
    assert _resolve_ladder(None, Accelerator.A100, Accelerator.T4) == [Accelerator.A100]
    assert _resolve_ladder("A100, A100 ,T4", None, Accelerator.T4) == [
        Accelerator.A100,
        Accelerator.T4,
    ]


async def test_allocate_ladder_falls_through_stockout():
    t = _LadderTransport(unavailable={Accelerator.A100, Accelerator.L4})
    session = await ColabClient(transport=t).allocate(gpu="A100,L4,T4", keep=True)
    assert session.info is not None and session.info.accelerator is Accelerator.T4
    assert t.attempted == [Accelerator.A100, Accelerator.L4, Accelerator.T4]


async def test_allocate_ladder_all_unavailable_raises_last():
    t = _LadderTransport(unavailable={Accelerator.A100, Accelerator.H100})
    with pytest.raises(AcceleratorUnavailableError):
        await ColabClient(transport=t).allocate(gpu="A100,H100")


# -- quota --------------------------------------------------------------------


async def test_quota_returns_ccu_info():
    class _Ccu(FakeTransport):
        async def ccu_info(self):
            return {"computeUnits": 42.5}

    assert await ColabClient(transport=_Ccu()).quota() == {"computeUnits": 42.5}


async def test_quota_none_when_unsupported():
    assert await ColabClient(transport=FakeTransport()).quota() is None


# -- transport selection + browser start --------------------------------------


def test_build_browser_transport():
    from colabctl.transport.browser import BrowserBridgeTransport

    assert isinstance(ColabClient(transport_name="browser").transport, BrowserBridgeTransport)


def test_unknown_transport_message_lists_browser():
    with pytest.raises(ConfigurationError, match="browser"):
        ColabClient(transport_name="bogus")


async def test_context_manager_starts_transport_when_supported():
    started: list[bool] = []

    class _Startable(FakeTransport):
        async def start(self) -> None:
            started.append(True)

    async with ColabClient(transport=_Startable()):
        pass
    assert started == [True]  # browser-style transports get their async start()
