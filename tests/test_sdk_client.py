"""Tests for ColabClient + ColabSession against the FakeTransport."""

from __future__ import annotations

import pytest

from colabctl.errors import ConfigurationError
from colabctl.models import Accelerator
from colabctl.sdk import ColabClient
from conftest import FakeTransport


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
