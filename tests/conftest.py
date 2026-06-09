"""Shared test doubles."""

from __future__ import annotations

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

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport()


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
