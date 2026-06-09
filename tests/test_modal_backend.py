"""Tests for the Modal backend: accelerator mapping + orchestration vs a fake modal SDK.

The fake mirrors the documented async Modal Sandbox API
(``Image.debian_slim().pip_install`` · ``App.lookup.aio`` · ``Sandbox.create.aio`` ·
``sandbox.exec.aio`` → ``proc.stdout.read.aio`` / ``proc.wait.aio`` / ``proc.returncode`` ·
``sandbox.terminate.aio``), so the backend's orchestration is exercised without a Modal account.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from colabctl.backends.base import JobSpec, JobState
from colabctl.backends.modal_backend import ModalBackend, modal_gpu
from colabctl.errors import ConfigurationError
from colabctl.models import Accelerator


def test_modal_gpu_mapping():
    assert modal_gpu(Accelerator.T4) == "T4"
    assert modal_gpu(Accelerator.A100) == "A100"
    assert modal_gpu(Accelerator.H100) == "H100"
    assert modal_gpu(Accelerator.NONE) is None
    with pytest.raises(ConfigurationError):
        modal_gpu(Accelerator.V5E1)  # TPU unsupported on Modal


# --- fake modal SDK ---------------------------------------------------------


class _Read:
    def __init__(self, value):
        self._value = value

    async def aio(self):
        return self._value


class _Wait:
    def __init__(self, proc, rc):
        self._proc = proc
        self._rc = rc

    async def aio(self):
        self._proc.returncode = self._rc


class FakeProc:
    def __init__(self, out, err, rc):
        self.stdout = SimpleNamespace(read=_Read(out))
        self.stderr = SimpleNamespace(read=_Read(err))
        self.returncode = None
        self.wait = _Wait(self, rc)


class _Exec:
    def __init__(self, proc):
        self._proc = proc
        self.calls = []

    async def aio(self, *cmd):
        self.calls.append(cmd)
        return self._proc


class _Terminate:
    def __init__(self):
        self.terminated = False

    async def aio(self, wait=False):
        self.terminated = True


class FakeSandbox:
    def __init__(self, proc):
        self.object_id = "sb-fake"
        self.exec = _Exec(proc)
        self.terminate = _Terminate()


class _Create:
    def __init__(self, sandbox):
        self._sandbox = sandbox
        self.kwargs = None

    async def aio(self, **kwargs):
        self.kwargs = kwargs
        return self._sandbox


class _Lookup:
    async def aio(self, name, create_if_missing=False):
        return SimpleNamespace(name=name)


class FakeImage:
    def __init__(self):
        self.installed = []

    def pip_install(self, *pkgs):
        self.installed.extend(pkgs)
        return self


def make_fake_modal(out="OUT\n", err="", rc=0):
    proc = FakeProc(out, err, rc)
    sandbox = FakeSandbox(proc)
    image = FakeImage()
    create = _Create(sandbox)
    modal = SimpleNamespace(
        Image=SimpleNamespace(debian_slim=lambda python_version=None: image),
        App=SimpleNamespace(lookup=_Lookup()),
        Sandbox=SimpleNamespace(create=create),
    )
    return modal, sandbox, image, create


async def test_modal_run_success(monkeypatch):
    modal, sandbox, image, create = make_fake_modal(out="hello gpu\n", rc=0)
    monkeypatch.setattr("colabctl.backends.modal_backend._load_modal", lambda: modal)
    backend = ModalBackend()
    result = await backend.run(
        JobSpec(code="print('hi')", accelerator=Accelerator.A100, requirements=["torch"])
    )
    assert result.ok
    assert result.state is JobState.SUCCEEDED
    assert result.stdout == "hello gpu\n"
    assert result.exit_code == 0
    assert "torch" in image.installed
    assert create.kwargs["gpu"] == "A100"
    assert sandbox.exec.calls == [("python", "-c", "print('hi')")]
    assert sandbox.terminate.terminated  # always torn down


async def test_modal_run_nonzero_exit_is_failed(monkeypatch):
    modal, sandbox, _, _ = make_fake_modal(out="", err="Traceback...\n", rc=1)
    monkeypatch.setattr("colabctl.backends.modal_backend._load_modal", lambda: modal)
    backend = ModalBackend()
    result = await backend.run(JobSpec(code="raise SystemExit(1)", accelerator=Accelerator.T4))
    assert not result.ok
    assert result.state is JobState.FAILED
    assert result.exit_code == 1
    assert "Traceback" in (result.error or "")
    assert sandbox.terminate.terminated


async def test_modal_cpu_omits_gpu_kwarg(monkeypatch):
    modal, _, _, create = make_fake_modal()
    monkeypatch.setattr("colabctl.backends.modal_backend._load_modal", lambda: modal)
    backend = ModalBackend()
    await backend.run(JobSpec(code="print(1)", accelerator=Accelerator.NONE))
    assert "gpu" not in create.kwargs


async def test_modal_spend_guard_caps_timeout(monkeypatch):
    modal, _, _, create = make_fake_modal()
    monkeypatch.setattr("colabctl.backends.modal_backend._load_modal", lambda: modal)
    backend = ModalBackend(max_timeout=3600)
    await backend.run(JobSpec(code="x=1", accelerator=Accelerator.T4, timeout=99999))
    assert create.kwargs["timeout"] == 3600  # capped by the spend guard
