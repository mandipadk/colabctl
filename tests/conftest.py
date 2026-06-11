"""Shared test doubles."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from colabctl.backends.base import (
    Backend,
    BackendCapabilities,
    JobInfo,
    JobResult,
    JobSpec,
    JobState,
)
from colabctl.models import ExecutionResult, RuntimeSpec, SessionInfo, SessionStatus, StreamOutput
from colabctl.transport.base import Capabilities, OutputCallback, TransportAdapter


@pytest.fixture(autouse=True)
def _isolate_colabctl_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the colabctl state store at a per-test temp dir.

    The native transport now persists every allocation under ``$COLABCTL_HOME`` — this
    autouse fixture keeps that out of the developer's real ``~/.colabctl`` and makes
    state hermetic across tests (each test gets its own empty store).
    """
    monkeypatch.setenv("COLABCTL_HOME", str(tmp_path / "colabctl-home"))


@pytest.fixture(autouse=True)
def _no_real_secret_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let tests touch the real OS keychain.

    The native transport defaults to discovering an OS keychain to cache proxy tokens;
    in tests we force "no default store" so ``secrets=None`` means *no cache* (attach
    takes the refresh path). Tests that exercise the cached path pass an explicit
    in-memory store.
    """
    monkeypatch.setattr("colabctl.transport.native.adapter._try_default_secrets", lambda: None)


class FakeTransport(TransportAdapter):
    """In-memory TransportAdapter for SDK/CLI tests (no network, no subprocess)."""

    name = "fake"

    def __init__(self, *, execute_text: str | None = None) -> None:
        self.sessions: dict[str, SessionInfo] = {}
        self.executed: list[tuple[str, str]] = []
        self.uploaded: list[tuple[str, str, str]] = []
        self.downloaded: list[tuple[str, str, str]] = []
        self.stopped: list[str] = []
        self.keepalives: list[str] = []
        self.interrupts: list[str] = []
        self.closed = False
        self._execute_text = execute_text

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(name=self.name)

    async def allocate(self, spec: RuntimeSpec) -> SessionInfo:
        name = spec.name or "fake-sess"
        info = SessionInfo(
            name=name,
            endpoint=f"ep-{name}",
            accelerator=spec.accelerator,
            variant=spec.variant,
            status=SessionStatus.IDLE,
        )
        self.sessions[name] = info
        return info

    async def list_sessions(self) -> list[SessionInfo]:
        return list(self.sessions.values())

    async def status(self, name: str) -> SessionInfo | None:
        return self.sessions.get(name)

    async def execute(
        self,
        name: str,
        code: str,
        *,
        timeout: float | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecutionResult:
        self.executed.append((name, code))
        text = self._execute_text if self._execute_text is not None else f"ran:{code[:24]}"
        result = ExecutionResult(status="ok", outputs=[StreamOutput(name="stdout", text=text)])
        if on_output is not None:
            for o in result.outputs:
                on_output(o)
        return result

    async def upload(self, name: str, local_path: Path, remote_path: str) -> None:
        self.uploaded.append((name, str(local_path), remote_path))

    async def download(self, name: str, remote_path: str, local_path: Path) -> None:
        self.downloaded.append((name, remote_path, str(local_path)))
        local_path.write_text("downloaded")

    async def stop(self, name: str) -> None:
        self.stopped.append(name)
        self.sessions.pop(name, None)

    async def keep_alive(self, name: str) -> None:
        self.keepalives.append(name)

    async def interrupt(self, name: str) -> None:
        self.interrupts.append(name)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport()


class LocalExecTransport(FakeTransport):
    """A transport whose ``execute`` runs the payload as a real local subprocess.

    Lets the detached-job substrate (which emits pure-stdlib Python) be exercised
    end-to-end and hermetically — same code paths the native transport runs on Colab,
    minus the network. ``allocate`` etc. come from :class:`FakeTransport`.
    """

    name = "localexec"

    async def execute(self, name, code, *, timeout=None, on_output=None) -> ExecutionResult:
        proc = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        status = "ok" if proc.returncode == 0 else "error"
        outputs = [StreamOutput(name="stdout", text=proc.stdout)]
        if proc.stderr:
            outputs.append(StreamOutput(name="stderr", text=proc.stderr))
        return ExecutionResult(status=status, outputs=outputs)


class FakeBackend(Backend):
    """In-memory Backend for CLI/MCP/router tests."""

    def __init__(self, name="fake", *, accels=None, result=None):
        self.name = name
        self._accels = accels if accels is not None else ["T4"]
        self._result = result
        self.specs: list[JobSpec] = []
        self.closed = False

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            name=self.name, accelerators=self._accels, tos_posture="sanctioned"
        )

    async def submit(self, spec: JobSpec) -> JobInfo:
        self.specs.append(spec)
        return JobInfo(id="j", backend=self.name, state=JobState.PENDING)

    async def status(self, job_id: str) -> JobInfo:
        return JobInfo(id=job_id, backend=self.name, state=JobState.SUCCEEDED)

    async def logs(self, job_id: str) -> str:
        return "log"

    async def result(self, job_id: str) -> JobResult:
        return self._result or JobResult(id=job_id, backend=self.name, state=JobState.SUCCEEDED)

    async def run(self, spec: JobSpec) -> JobResult:
        self.specs.append(spec)
        return self._result or JobResult(
            id="j", backend=self.name, state=JobState.SUCCEEDED, stdout="out"
        )

    async def cancel(self, job_id: str) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True
