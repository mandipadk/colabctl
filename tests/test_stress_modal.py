"""Adversarial tests for ModalBackend: failure, cancel, unknown-job, bad accelerator."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from colabctl.backends.base import JobSpec, JobState
from colabctl.backends.modal_backend import ModalBackend
from colabctl.errors import ColabctlError, ConfigurationError
from colabctl.models import Accelerator


class _Img:
    def pip_install(self, *pkgs):
        return self


async def _lookup_aio(name, create_if_missing=False):
    return SimpleNamespace(name=name)


def _modal(create_aio):
    return SimpleNamespace(
        Image=SimpleNamespace(debian_slim=lambda python_version=None: _Img()),
        App=SimpleNamespace(lookup=SimpleNamespace(aio=_lookup_aio)),
        Sandbox=SimpleNamespace(create=SimpleNamespace(aio=create_aio)),
    )


class HangingSandbox:
    def __init__(self):
        self.object_id = "sb"
        self.terminated = False
        self.exec = SimpleNamespace(aio=self._exec)
        self.terminate = SimpleNamespace(aio=self._terminate)

    async def _exec(self, *cmd):
        await asyncio.sleep(3600)  # never completes until cancelled

    async def _terminate(self, wait=False):
        self.terminated = True


async def test_execute_exception_marks_failed(monkeypatch):
    async def create_raises(**kwargs):
        raise RuntimeError("modal is down")

    monkeypatch.setattr(
        "colabctl.backends.modal_backend._load_modal", lambda: _modal(create_raises)
    )
    result = await ModalBackend().run(JobSpec(code="x = 1", accelerator=Accelerator.T4))
    assert result.state is JobState.FAILED
    assert "RuntimeError: modal is down" in (result.error or "")


async def test_cancel_marks_cancelled_and_tears_down(monkeypatch):
    sandbox = HangingSandbox()

    async def create_ok(**kwargs):
        return sandbox

    monkeypatch.setattr("colabctl.backends.modal_backend._load_modal", lambda: _modal(create_ok))
    backend = ModalBackend()
    info = await backend.submit(JobSpec(code="while True: pass", accelerator=Accelerator.T4))
    await asyncio.sleep(0.02)  # let the task reach the hanging exec
    await backend.cancel(info.id)
    result = await backend.result(info.id)
    assert result.state is JobState.CANCELLED
    assert sandbox.terminated  # finally block ran best-effort teardown


async def test_result_unknown_job_raises():
    with pytest.raises(ColabctlError):
        await ModalBackend().result("does-not-exist")


async def test_status_unknown_job_raises():
    with pytest.raises(ColabctlError):
        await ModalBackend().status("nope")


async def test_submit_unsupported_accelerator_raises():
    # modal_gpu validates before any SDK import, so no monkeypatch needed.
    with pytest.raises(ConfigurationError):
        await ModalBackend().submit(JobSpec(code="x = 1", accelerator=Accelerator.G4))


async def test_aclose_cancels_inflight(monkeypatch):
    sandbox = HangingSandbox()

    async def create_ok(**kwargs):
        return sandbox

    monkeypatch.setattr("colabctl.backends.modal_backend._load_modal", lambda: _modal(create_ok))
    backend = ModalBackend()
    info = await backend.submit(JobSpec(code="loop", accelerator=Accelerator.T4))
    await asyncio.sleep(0.02)
    await backend.aclose()
    await asyncio.sleep(0.01)  # let cancellation propagate
    job = backend._jobs[info.id]
    assert (job.task is not None and job.task.cancelled()) or job.task.done()
