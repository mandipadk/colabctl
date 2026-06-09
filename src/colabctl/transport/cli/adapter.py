"""``ColabCliTransport`` — the sanctioned-default transport over ``google-colab-cli``.

Invokes the ``colab`` executable as an async subprocess and parses its human
stdout via :mod:`colabctl.transport.cli.parser`. Auth defaults to ``adc`` (the
Phase 0-verified working path). This transport is intentionally a *thin* adapter:
all output understanding lives in the (golden-tested) parser, and the heavier,
structured-output path lives in the native transport.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from colabctl.errors import CLIError
from colabctl.models import (
    Accelerator,
    ExecutionResult,
    RuntimeSpec,
    SessionInfo,
    StreamOutput,
)
from colabctl.observability import get_logger
from colabctl.transport.base import Capabilities, OutputCallback, TransportAdapter
from colabctl.transport.cli import parser

_DEFAULT_PROCESS_TIMEOUT = 300.0  # seconds; allocation can take a while
_log = get_logger("transport.cli")


class ColabCliTransport(TransportAdapter):
    """Drive Colab through the official ``google-colab-cli``."""

    name = "cli"

    def __init__(
        self,
        *,
        colab_bin: str = "colab",
        auth: str = "adc",
        config_path: str | None = None,
        process_timeout: float = _DEFAULT_PROCESS_TIMEOUT,
    ) -> None:
        self._bin = colab_bin
        self._auth = auth
        self._config_path = config_path
        self._process_timeout = process_timeout
        self._probed = False

    # -- contract -----------------------------------------------------------

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            name=self.name,
            interactive=True,
            streaming_output=False,
            headless=True,
            selectable_accelerator=True,
            keepalive=False,
            file_transfer=True,
            notebook_execution=True,
            caveats=[
                "Keep-alive is unavailable under ADC (serviceusage 403, Phase 0 §2); "
                "long-running sessions are reclaimed at Colab's idle timeout.",
                "No machine-readable output; stdout is parsed against pinned "
                f"CLI v{parser.PINNED_CLI_VERSION}.",
                "Rich outputs (images, dataframes) are not structured via the CLI — "
                "use the native transport for typed Jupyter outputs.",
            ],
        )

    async def allocate(self, spec: RuntimeSpec) -> SessionInfo:
        name = spec.name or f"cc-{uuid.uuid4().hex[:8]}"
        args = ["new", "-s", name, *self._accelerator_args(spec.accelerator)]
        rc, out, err = await self._run(args)
        if rc != 0:
            parser.raise_for_known_errors(stdout=out, stderr=err, returncode=rc, argv=args)
            raise CLIError(
                f"`colab new` failed (exit {rc})", argv=args, returncode=rc, stdout=out, stderr=err
            )
        _, ready = parser.parse_new_output(out)
        if not ready:
            raise CLIError(
                "`colab new` did not report 'Session READY.'",
                argv=args,
                returncode=rc,
                stdout=out,
                stderr=err,
            )
        info = await self.status(name)
        if info is None:
            # Allocation succeeded but status didn't echo it back — synthesize a
            # minimal record so the caller still has a handle.
            return SessionInfo(
                name=name,
                endpoint="",
                accelerator=spec.accelerator,
                variant=spec.variant,
            )
        return info

    async def list_sessions(self) -> list[SessionInfo]:
        rc, out, err = await self._run(["sessions"])
        if rc != 0:
            raise CLIError(
                f"`colab sessions` failed (exit {rc})",
                argv=["sessions"],
                returncode=rc,
                stdout=out,
                stderr=err,
            )
        return parser.parse_sessions_output(out)

    async def status(self, name: str) -> SessionInfo | None:
        rc, out, err = await self._run(["status", "-s", name])
        if rc != 0:
            raise CLIError(
                f"`colab status` failed (exit {rc})",
                argv=["status", "-s", name],
                returncode=rc,
                stdout=out,
                stderr=err,
            )
        sessions = parser.parse_status_output(out)
        for s in sessions:
            if s.name == name:
                return s
        return sessions[0] if sessions else None

    async def execute(
        self,
        name: str,
        code: str,
        *,
        timeout: float | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecutionResult:
        # The CLI reads code from stdin; it prints program output to stdout
        # intermixed with no structured framing, so we capture it as a single
        # stdout stream. (Typed per-output results come from the native transport.)
        rc, out, err = await self._run(["exec", "-s", name], stdin=code.encode(), timeout=timeout)
        outputs: list[StreamOutput] = []
        if out:
            outputs.append(StreamOutput(name="stdout", text=out))
        if err:
            outputs.append(StreamOutput(name="stderr", text=err))
        if on_output is not None:
            for o in outputs:
                on_output(o)
        return ExecutionResult(status="ok" if rc == 0 else "error", outputs=list(outputs))

    async def upload(self, name: str, local_path: Path, remote_path: str) -> None:
        args = ["upload", "-s", name, str(local_path), remote_path]
        rc, out, err = await self._run(args)
        if rc != 0 or not parser.parse_upload_ok(out):
            raise CLIError(
                f"upload failed (exit {rc})", argv=args, returncode=rc, stdout=out, stderr=err
            )

    async def download(self, name: str, remote_path: str, local_path: Path) -> None:
        args = ["download", "-s", name, remote_path, str(local_path)]
        rc, out, err = await self._run(args)
        if rc != 0 or not parser.parse_download_ok(out):
            raise CLIError(
                f"download failed (exit {rc})", argv=args, returncode=rc, stdout=out, stderr=err
            )

    async def stop(self, name: str) -> None:
        args = ["stop", "-s", name]
        rc, out, err = await self._run(args)
        if rc != 0:
            raise CLIError(
                f"`colab stop` failed (exit {rc})",
                argv=args,
                returncode=rc,
                stdout=out,
                stderr=err,
            )

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _accelerator_args(accelerator: Accelerator) -> list[str]:
        if accelerator is Accelerator.NONE:
            return []
        if accelerator.is_tpu:
            return ["--tpu", accelerator.value.lower()]
        return ["--gpu", accelerator.value]

    def _global_args(self) -> list[str]:
        args = ["--auth", self._auth]
        if self._config_path is not None:
            args += ["--config", self._config_path]
        return args

    async def _ensure_probed(self) -> None:
        """Probe `colab version` once; warn (don't fail) if it drifts from the pin."""
        if self._probed:
            return
        self._probed = True
        try:
            _, out, _ = await self._run(["version"])
        except CLIError:
            return  # never block real work on a failed probe
        version = parser.parse_version(out)
        if version and version != parser.PINNED_CLI_VERSION:
            _log.warning(
                "google-colab-cli %s differs from the pinned %s; stdout parsing may drift "
                "(pin `google-colab-cli==%s` or update the parser).",
                version,
                parser.PINNED_CLI_VERSION,
                parser.PINNED_CLI_VERSION,
            )

    async def _run(
        self,
        args: list[str],
        *,
        stdin: bytes | None = None,
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        if args and args[0] != "version":
            await self._ensure_probed()
        argv = [self._bin, *self._global_args(), *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise CLIError(
                f"`{self._bin}` not found on PATH. Install it with "
                "`uv tool install --python 3.13 google-colab-cli`.",
                argv=argv,
            ) from exc

        budget = timeout if timeout is not None else self._process_timeout
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(stdin), timeout=budget)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise CLIError(f"`colab {args[0]}` timed out after {budget}s", argv=argv) from exc
        return (
            proc.returncode if proc.returncode is not None else -1,
            stdout_b.decode(errors="replace"),
            stderr_b.decode(errors="replace"),
        )
