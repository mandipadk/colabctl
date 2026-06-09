"""``colabctl`` command-line interface (Typer), built on the SDK.

Every command runs through the same :class:`ColabClient` the SDK exposes, so the
CLI and library behave identically. ``--transport`` chooses ``cli`` (sanctioned
default) or ``native`` (opt-in). The client factory is module-level so tests can
inject a fake transport.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import typer

from colabctl import __version__
from colabctl.backends.base import Backend, JobSpec
from colabctl.backends.factory import BACKEND_NAMES, build_backend
from colabctl.errors import ColabctlError
from colabctl.models import Accelerator, SessionInfo
from colabctl.sdk.client import ColabClient, _resolve_accelerator

app = typer.Typer(
    name="colabctl",
    help="Programmatic control of Google Colab — run code, manage runtimes, sync files.",
    no_args_is_help=True,
    add_completion=False,
)

_T = TypeVar("_T")


@dataclass
class _State:
    transport: str
    auth: str
    colab_bin: str


@app.callback()
def _root(
    ctx: typer.Context,
    transport: str = typer.Option("cli", "--transport", "-t", help="Transport: cli | native"),
    auth: str = typer.Option("adc", "--auth", help="Auth strategy for the CLI transport"),
    colab_bin: str = typer.Option("colab", "--colab-bin", help="Path to the `colab` executable"),
) -> None:
    ctx.obj = _State(transport=transport, auth=auth, colab_bin=colab_bin)


def _make_client(state: _State) -> ColabClient:
    """Build a client for the chosen transport (patched in tests)."""
    return ColabClient(
        transport_name=state.transport, auth_mode=state.auth, colab_bin=state.colab_bin
    )


def _run(coro: Coroutine[Any, Any, _T]) -> _T:
    try:
        return asyncio.run(coro)
    except ColabctlError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc


def _fmt_session(info: SessionInfo) -> str:
    line = (
        f"[{info.name}] {info.endpoint} | Hardware: {info.hardware_label} "
        f"| Variant: {info.variant.value}"
    )
    if info.status.value != "UNKNOWN":
        line += f" | Status: {info.status.value}"
    return line


def _emit(result_text: str, stderr_text: str) -> None:
    if result_text:
        typer.echo(result_text, nl=not result_text.endswith("\n"))
    if stderr_text:
        typer.echo(stderr_text, err=True, nl=not stderr_text.endswith("\n"))


@app.command()
def version() -> None:
    """Print the colabctl version."""
    typer.echo(f"colabctl {__version__}")


@app.command()
def run(
    ctx: typer.Context,
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Local .py file to run"),
    gpu: str = typer.Option("T4", "--gpu", help="Accelerator (T4/L4/A100/H100/...)"),
    keep: bool = typer.Option(False, "--keep", help="Leave the runtime running afterwards"),
    timeout: float | None = typer.Option(None, "--timeout", help="Execution timeout (seconds)"),
) -> None:
    """Allocate a runtime, run a local file on it, then release it (unless --keep)."""
    state: _State = ctx.obj

    async def _go() -> None:
        async with _make_client(state) as client:
            session = await client.allocate(gpu=gpu, keep=keep)
            async with session:
                result = await session.run_file(file, timeout=timeout)
                _emit(result.text, result.stderr)
                if not result.ok:
                    raise typer.Exit(1)

    _run(_go())


@app.command(name="exec")
def exec_(
    ctx: typer.Context,
    session: str = typer.Option(..., "--session", "-s", help="Existing session name"),
    code: str | None = typer.Option(None, "--code", "-c", help="Code to run (else read stdin)"),
    timeout: float | None = typer.Option(None, "--timeout"),
) -> None:
    """Run code on an existing session (from --code or stdin)."""
    state: _State = ctx.obj
    source = code if code is not None else sys.stdin.read()

    async def _go() -> None:
        async with _make_client(state) as client:
            result = await client.attach(session).run(source, timeout=timeout)
            _emit(result.text, result.stderr)
            if not result.ok:
                raise typer.Exit(1)

    _run(_go())


@app.command()
def new(
    ctx: typer.Context,
    gpu: str = typer.Option("T4", "--gpu"),
    name: str | None = typer.Option(None, "--name", "-s", help="Session name"),
) -> None:
    """Allocate a runtime and leave it running (attach later with `exec -s`)."""
    state: _State = ctx.obj

    async def _go() -> None:
        async with _make_client(state) as client:
            session = await client.allocate(gpu=gpu, name=name, keep=True)
            info = await session.status() or session.info
            if info is not None:
                typer.echo(_fmt_session(info))
            else:
                typer.echo(f"[{session.name}] READY")

    _run(_go())


@app.command()
def sessions(ctx: typer.Context) -> None:
    """List active sessions."""
    state: _State = ctx.obj

    async def _go() -> None:
        async with _make_client(state) as client:
            items = await client.list_sessions()
            if not items:
                typer.echo("No active sessions.")
                return
            for info in items:
                typer.echo(_fmt_session(info))

    _run(_go())


@app.command()
def status(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Show one session's status."""
    state: _State = ctx.obj

    async def _go() -> None:
        async with _make_client(state) as client:
            info = await client.attach(name).status()
            typer.echo(_fmt_session(info) if info is not None else f"Session {name!r} not found.")

    _run(_go())


@app.command()
def stop(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Stop a session and release its runtime."""
    state: _State = ctx.obj

    async def _go() -> None:
        async with _make_client(state) as client:
            await client.attach(name).stop()
            typer.echo(f"Stopped {name}.")

    _run(_go())


@app.command()
def upload(
    ctx: typer.Context,
    session: str = typer.Argument(...),
    local: Path = typer.Argument(..., exists=True, dir_okay=False),
    remote: str = typer.Argument(...),
) -> None:
    """Upload a local file to a session's runtime."""
    state: _State = ctx.obj

    async def _go() -> None:
        async with _make_client(state) as client:
            await client.attach(session).upload(local, remote)
            typer.echo(f"Uploaded {local} -> {remote}")

    _run(_go())


@app.command()
def download(
    ctx: typer.Context,
    session: str = typer.Argument(...),
    remote: str = typer.Argument(...),
    local: Path = typer.Argument(...),
) -> None:
    """Download a file from a session's runtime."""
    state: _State = ctx.obj

    async def _go() -> None:
        async with _make_client(state) as client:
            await client.attach(session).download(remote, local)
            typer.echo(f"Downloaded {remote} -> {local}")

    _run(_go())


@app.command()
def keepalive(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Send a keep-alive to a session (native transport only)."""
    state: _State = ctx.obj

    async def _go() -> None:
        async with _make_client(state) as client:
            await client.attach(name).keep_alive()
            typer.echo(f"Keep-alive sent to {name}.")

    _run(_go())


job_app = typer.Typer(
    name="job",
    help="Run batch jobs across backends (colab | modal | vertex).",
    no_args_is_help=True,
)
app.add_typer(job_app, name="job")


def _make_backend(name: str, state: _State) -> Backend:
    """Build a backend by name (patched in tests)."""
    return build_backend(
        name, transport_name=state.transport, auth_mode=state.auth, colab_bin=state.colab_bin
    )


@job_app.command(name="run")
def job_run(
    ctx: typer.Context,
    file: Path | None = typer.Argument(
        None, exists=True, dir_okay=False, help="Local .py file to run (or use --code)"
    ),
    code: str | None = typer.Option(None, "--code", "-c", help="Inline code (instead of a file)"),
    backend: str = typer.Option(
        "colab", "--backend", "-b", help=f"One of: {', '.join(BACKEND_NAMES)}"
    ),
    gpu: str = typer.Option("T4", "--gpu", help="Accelerator (T4/L4/A100/H100, or 'none' for CPU)"),
    requirement: list[str] = typer.Option([], "--req", "-r", help="pip requirement (repeatable)"),
    timeout: int | None = typer.Option(None, "--timeout", help="Job timeout (seconds)"),
) -> None:
    """Run a job on a backend, wait for it, and print the result."""
    state: _State = ctx.obj
    if (file is None) == (code is None):
        typer.secho("error: provide exactly one of FILE or --code", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    if gpu.lower() == "none":
        accelerator = Accelerator.NONE
    else:
        accelerator = _resolve_accelerator(gpu, None, default=Accelerator.T4)
    spec = JobSpec(
        code=code,
        script_path=str(file) if file is not None else None,
        accelerator=accelerator,
        requirements=list(requirement),
        timeout=timeout,
    )

    async def _go() -> None:
        backend_obj = _make_backend(backend, state)
        try:
            result = await backend_obj.run(spec)
            _emit(result.stdout, result.stderr)
            typer.echo(f"[{result.backend}] {result.state.value}", err=True)
            if not result.ok:
                if result.error:
                    typer.secho(f"error: {result.error}", fg=typer.colors.RED, err=True)
                raise typer.Exit(1)
        finally:
            await backend_obj.aclose()

    _run(_go())


@job_app.command(name="backends")
def job_backends(ctx: typer.Context) -> None:
    """List available backends and their capabilities."""
    state: _State = ctx.obj
    for name in BACKEND_NAMES:
        caps = _make_backend(name, state).capabilities
        accels = ", ".join(sorted(set(caps.accelerators))) or "any"
        typer.echo(f"{name}: gpus=[{accels}] tos={caps.tos_posture} interactive={caps.interactive}")


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
