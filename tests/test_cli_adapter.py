"""Tests for the CLI transport's capability/version probe."""

from __future__ import annotations

from colabctl.transport.cli import parser
from colabctl.transport.cli.adapter import ColabCliTransport


def _fake_run(version_line: str):
    async def run(args, *, stdin=None, timeout=None):
        return (0, version_line, "")

    return run


async def test_probe_warns_on_version_drift(monkeypatch, caplog):
    transport = ColabCliTransport()
    monkeypatch.setattr(transport, "_run", _fake_run("Version: 9.9.9"))
    with caplog.at_level("WARNING", logger="colabctl.transport.cli"):
        await transport._ensure_probed()
    assert any("differs from the pinned" in r.message for r in caplog.records)


async def test_probe_silent_when_pinned(monkeypatch, caplog):
    transport = ColabCliTransport()
    monkeypatch.setattr(transport, "_run", _fake_run(f"Version: {parser.PINNED_CLI_VERSION}"))
    with caplog.at_level("WARNING", logger="colabctl.transport.cli"):
        await transport._ensure_probed()
    assert not any("differs" in r.message for r in caplog.records)


async def test_probe_runs_only_once(monkeypatch):
    transport = ColabCliTransport()
    calls = {"n": 0}

    async def run(args, *, stdin=None, timeout=None):
        calls["n"] += 1
        return (0, f"Version: {parser.PINNED_CLI_VERSION}", "")

    monkeypatch.setattr(transport, "_run", run)
    await transport._ensure_probed()
    await transport._ensure_probed()
    assert calls["n"] == 1  # probed once, cached
