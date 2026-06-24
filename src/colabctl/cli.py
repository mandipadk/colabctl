"""``colabctl`` command-line interface (Typer), built on the SDK.

Every command runs through the same :class:`ColabClient` the SDK exposes, so the
CLI and library behave identically. ``--transport`` chooses ``cli`` (sanctioned
default) or ``native`` (opt-in). The client factory is module-level so tests can
inject a fake transport.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import typer

from colabctl import __version__
from colabctl.auth.base import ADC_LOGIN_SCOPES, AuthProvider
from colabctl.auth.diagnostics import COLABORATORY_SCOPE, DRIVE_FILE_SCOPE, scopes_of, token_info
from colabctl.backends.base import Backend, JobResult, JobSpec
from colabctl.backends.factory import BACKEND_NAMES, build_backend, build_router
from colabctl.backends.router import BackendRouter
from colabctl.errors import ColabctlError, TooManyAssignmentsError
from colabctl.models import Accelerator, CcuInfo, ExecutionResult, Output, SessionInfo
from colabctl.sdk.client import ColabClient, _resolve_accelerator, _resolve_ladder
from colabctl.spend import spend_report
from colabctl.transport.native import NativeColabTransport

if TYPE_CHECKING:
    from colabctl.jobs.backend import DetachedColabBackend

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
    transport: str = typer.Option(
        "cli", "--transport", "-t", help="Transport: cli | native | browser"
    ),
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
    except TooManyAssignmentsError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        typer.secho(
            "Reclaim orphaned runtimes with:  colabctl -t native gc --release-orphans",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(1) from exc
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


class _StreamPrinter:
    """An ``on_output`` callback that prints outputs live as they stream in.

    Records whether it printed anything, so the caller can fall back to a single buffered
    emit for transports that don't stream (then the output isn't lost or doubled).
    """

    def __init__(self) -> None:
        self.printed = False

    def __call__(self, output: Output) -> None:
        text = getattr(output, "text", None)
        if not text:
            return
        self.printed = True
        typer.echo(text, nl=False, err=getattr(output, "name", "") == "stderr")


def _finish_run(result: ExecutionResult, printer: _StreamPrinter) -> None:
    """Finish a `run`/`exec`: if output streamed live, just cap it (and surface a concise
    error on failure); otherwise emit the buffered result once (non-streaming transports)."""
    if printer.printed:
        typer.echo("")  # newline to terminate the streamed output
        if not result.ok and result.error:
            typer.secho(f"error: {result.error}", fg=typer.colors.RED, err=True)
    else:
        _emit(result.text, result.stderr)
    if not result.ok:
        raise typer.Exit(1)


@app.command()
def version() -> None:
    """Print the colabctl version."""
    typer.echo(f"colabctl {__version__}")


auth_app = typer.Typer(
    name="auth",
    help="Set up and inspect Colab/Drive credentials (ADC).",
    no_args_is_help=True,
)
app.add_typer(auth_app, name="auth")


def _adc_provider() -> AuthProvider:
    """Build the ADC auth provider (patched in tests)."""
    from colabctl.auth import ADCAuthProvider

    return ADCAuthProvider()


@auth_app.command("scopes")
def auth_scopes() -> None:
    """Print the gcloud ADC login command with the scopes colabctl needs."""
    typer.echo("gcloud auth application-default login --scopes=" + ",".join(ADC_LOGIN_SCOPES))


@auth_app.command("login")
def auth_login() -> None:
    """Run the gcloud ADC login with colabctl's scopes (one-time per machine)."""
    cmd = [
        "gcloud",
        "auth",
        "application-default",
        "login",
        "--scopes=" + ",".join(ADC_LOGIN_SCOPES),
    ]
    typer.echo("running: " + " ".join(cmd), err=True)
    try:
        code = subprocess.call(cmd)
    except FileNotFoundError as exc:
        typer.secho(
            "gcloud not found on PATH. Install the Google Cloud SDK, then run:",
            fg=typer.colors.RED,
            err=True,
        )
        typer.echo(" ".join(cmd))
        raise typer.Exit(1) from exc
    raise typer.Exit(code)


@auth_app.command("status")
def auth_status() -> None:
    """Show the ADC account, scopes, quota project, and Colab/Drive readiness."""

    async def _go() -> None:
        provider = _adc_provider()
        try:
            token = await provider.token()
        except ColabctlError as exc:
            typer.secho(f"ADC not available: {exc}", fg=typer.colors.RED, err=True)
            typer.echo("Set it up with:  colabctl auth login")
            raise typer.Exit(1) from exc
        info = await token_info(token)
        scopes = scopes_of(info)
        has_colab = COLABORATORY_SCOPE in scopes
        has_drive = DRIVE_FILE_SCOPE in scopes
        quota = provider.quota_project_id
        typer.echo(f"account:       {info.get('email', '(unknown)')}")
        typer.echo(f"colaboratory:  {'yes' if has_colab else 'NO  (native transport will fail)'}")
        typer.echo(f"drive.file:    {'yes' if has_drive else 'NO  (Drive checkpoints will fail)'}")
        typer.echo(f"quota project: {quota or 'NOT SET  (Drive API calls will 403)'}")
        if not (has_colab and has_drive):
            typer.echo("→ fix scopes:  colabctl auth login")
        if quota is None:
            typer.echo(
                "→ fix quota:   gcloud auth application-default "
                "set-quota-project <PROJECT-with-Drive-API-enabled>"
            )

    _run(_go())


@app.command()
def quota(ctx: typer.Context) -> None:
    """Show Colab compute-unit balance, burn rate, runway, and entitled accelerators."""
    state: _State = ctx.obj

    async def _go() -> None:
        async with _make_client(state) as client:
            info = await client.quota()
            if info is None:
                typer.echo(
                    "Compute-unit info is only available on the native transport (-t native)."
                )
                return
            ccu = CcuInfo.from_raw(info)
            lines: list[str] = []
            if ccu is not None:
                if ccu.current_balance is not None:
                    lines.append(f"balance:     {ccu.current_balance:.2f} compute units")
                if ccu.consumption_rate_hourly is not None:
                    lines.append(f"burn rate:   {ccu.consumption_rate_hourly:.2f} / hour")
                if ccu.runway_hours is not None:
                    lines.append(f"runway:      ~{ccu.runway_hours:.1f} hours")
                if ccu.assignments_count is not None:
                    lines.append(f"assignments: {ccu.assignments_count}")
                if ccu.eligible_gpus:
                    lines.append(f"GPUs:        {', '.join(ccu.eligible_gpus)}")
                if ccu.eligible_tpus:
                    lines.append(f"TPUs:        {', '.join(ccu.eligible_tpus)}")
            if lines:
                for line in lines:
                    typer.echo(line)
            else:  # unknown shape — fall back to raw passthrough
                import json

                typer.echo(json.dumps(info, indent=2, default=str))

    _run(_go())


async def _spend_guard(client: ColabClient, gpu: str, yes: bool) -> None:
    """Refuse a native allocation that would likely fail on spend, unless ``yes``.

    No-op on non-native transports (no ``ccu-info``) or when the info is unavailable.
    """
    ccu = CcuInfo.from_raw(await client.quota())
    if ccu is None:
        return
    ladder = _resolve_ladder(gpu, None, default=Accelerator.T4)
    blockers, warnings = spend_report(ccu, ladder)
    for warning in warnings:
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW, err=True)
    if blockers and not yes:
        for blocker in blockers:
            typer.secho(f"blocked: {blocker}", fg=typer.colors.RED, err=True)
        typer.secho("Re-run with --yes to allocate anyway.", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(1)


@app.command()
def run(
    ctx: typer.Context,
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Local .py file to run"),
    gpu: str = typer.Option(
        "T4", "--gpu", help="Accelerator, or a fallback ladder like 'A100,L4,T4'"
    ),
    keep: bool = typer.Option(False, "--keep", help="Leave the runtime running afterwards"),
    timeout: float | None = typer.Option(None, "--timeout", help="Execution timeout (seconds)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the spend guard (allocate anyway)"),
) -> None:
    """Allocate a runtime, run a local file on it, then release it (unless --keep)."""
    state: _State = ctx.obj

    async def _go() -> None:
        async with _make_client(state) as client:
            await _spend_guard(client, gpu, yes)
            session = await client.allocate(gpu=gpu, keep=keep)
            async with session:
                printer = _StreamPrinter()
                result = await session.run_file(file, timeout=timeout, on_output=printer)
                _finish_run(result, printer)

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
            printer = _StreamPrinter()
            result = await client.attach(session).run(source, timeout=timeout, on_output=printer)
            _finish_run(result, printer)

    _run(_go())


@app.command()
def new(
    ctx: typer.Context,
    gpu: str = typer.Option("T4", "--gpu", help="Accelerator, or a ladder like 'A100,L4,T4'"),
    name: str | None = typer.Option(None, "--name", "-s", help="Session name"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the spend guard (allocate anyway)"),
) -> None:
    """Allocate a runtime and leave it running (attach later with `exec -s`)."""
    state: _State = ctx.obj

    async def _go() -> None:
        async with _make_client(state) as client:
            await _spend_guard(client, gpu, yes)
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


@app.command()
def interrupt(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Interrupt a session's running cell without killing the runtime (native transport)."""
    state: _State = ctx.obj

    async def _go() -> None:
        async with _make_client(state) as client:
            await client.attach(name).interrupt()
            typer.echo(f"Interrupted {name}.")

    _run(_go())


@app.command()
def attach(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Reconnect to a session created by another process (native transport).

    Reattaches via the GET-only refresh (fresh proxy token, verifies the runtime is
    still live) and prints the recovered session.
    """
    state: _State = ctx.obj

    async def _go() -> None:
        async with _make_client(state) as client:
            transport = client.transport
            if not isinstance(transport, NativeColabTransport):
                raise ColabctlError(
                    "`attach` is only supported on the native transport (-t native)."
                )
            info = await transport.attach(name)
            typer.echo(_fmt_session(info))

    _run(_go())


@app.command()
def gc(
    ctx: typer.Context,
    release_orphans: bool = typer.Option(
        False, "--release-orphans", help="Unassign live runtimes with no local record"
    ),
    prune: bool = typer.Option(
        True, "--prune/--no-prune", help="Drop records whose runtime is gone"
    ),
) -> None:
    """Reconcile local state with live runtimes; reclaim orphans and prune dead records.

    Native transport only. By default this is non-destructive to runtimes (it prunes
    stale local records and reports orphans); pass ``--release-orphans`` to unassign
    server-side runtimes that no process is tracking (the v0.2 leak class).
    """
    state: _State = ctx.obj

    async def _go() -> None:
        async with _make_client(state) as client:
            transport = client.transport
            if not isinstance(transport, NativeColabTransport):
                raise ColabctlError("`gc` is only supported on the native transport (-t native).")
            report = await transport.gc(release_orphans=release_orphans, prune_stale=prune)
            rec = report.reconcile
            typer.echo(
                f"reconcile: {len(rec.live_tracked)} tracked, "
                f"{len(rec.orphan_endpoints)} orphan(s), {len(rec.stale_sessions)} stale"
            )
            for endpoint in rec.orphan_endpoints:
                released = endpoint in report.released_orphans
                typer.echo(f"  orphan {endpoint} {'(released)' if released else '(left running)'}")
            for name in report.pruned_records:
                typer.echo(f"  pruned stale record {name}")
            if not release_orphans and rec.orphan_endpoints:
                typer.echo("  re-run with --release-orphans to unassign the orphan(s) above.")

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


def _make_router(names: list[str], state: _State) -> BackendRouter:
    """Build a capability-routing, infra-failover router over named backends (patched in tests)."""
    return build_router(
        names, transport_name=state.transport, auth_mode=state.auth, colab_bin=state.colab_bin
    )


def _print_job_result(result: JobResult) -> None:
    _emit(result.stdout, result.stderr)
    typer.echo(f"[{result.backend}] {result.state.value}", err=True)
    if not result.ok:
        if result.error:
            typer.secho(f"error: {result.error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


def _make_detached_backend(state: _State) -> DetachedColabBackend:
    """Build the durable detached Colab backend (native-only; patched in tests)."""
    from colabctl.jobs.backend import DetachedColabBackend

    return DetachedColabBackend.create(auth_mode=state.auth)


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
    detach: bool = typer.Option(
        False, "--detach", "-d", help="Submit a durable detached job and return its id (colab only)"
    ),
    resumable: bool = typer.Option(
        False, "--resumable", help="Mark a detached job auto-resumable after a runtime re-assign"
    ),
    allow: str | None = typer.Option(
        None,
        "--allow",
        help="Comma-separated backends to fail over across on infra errors, e.g. "
        "'colab,modal,vertex'. --backend is tried first; the job is RE-RUN on the next "
        "backend if one fails to allocate, so use this only for idempotent jobs.",
    ),
) -> None:
    """Run a job on a backend, wait for it, and print the result.

    With ``--detach`` the job is launched on Colab as a durable detached process and the
    command returns its id immediately — poll it later (from any process) with
    ``colabctl job status/logs/result``.
    """
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
        resumable=resumable,
    )

    if detach:
        if backend != "colab":
            typer.secho("error: --detach is only supported for the colab backend", err=True)
            raise typer.Exit(2)

        async def _go_detached() -> None:
            backend_obj = _make_detached_backend(state)
            try:
                info = await backend_obj.submit(spec)
                typer.echo(info.id)
                typer.echo(
                    f"[{info.id}] submitted ({info.detail}); follow with "
                    f"`colabctl job logs -f {info.id}`",
                    err=True,
                )
            finally:
                await backend_obj.aclose()

        _run(_go_detached())
        return

    async def _go() -> None:
        if allow:
            names = [n.strip() for n in allow.split(",") if n.strip()]
            router = _make_router(names, state)
            try:
                # --backend is the preferred first candidate; fail over to the rest on
                # infra errors (a ran-but-failed user job is never retried elsewhere).
                result = await router.run(spec, prefer=backend, fallback=True)
                _print_job_result(result)
            finally:
                await router.aclose()
        else:
            backend_obj = _make_backend(backend, state)
            try:
                _print_job_result(await backend_obj.run(spec))
            finally:
                await backend_obj.aclose()

    _run(_go())


@job_app.command(name="status")
def job_status(ctx: typer.Context, job_id: str = typer.Argument(...)) -> None:
    """Show a detached job's current state (cross-process)."""
    state: _State = ctx.obj

    async def _go() -> None:
        backend_obj = _make_detached_backend(state)
        try:
            info = await backend_obj.status(job_id)
            typer.echo(f"[{info.id}] {info.state.value} ({info.detail or ''})")
        finally:
            await backend_obj.aclose()

    _run(_go())


@job_app.command(name="logs")
def job_logs(
    ctx: typer.Context,
    job_id: str = typer.Argument(...),
    follow: bool = typer.Option(
        False, "--follow", "-f", help="Stream new output until the job ends"
    ),
) -> None:
    """Print a detached job's logs; ``--follow`` streams (and resumes by offset)."""
    state: _State = ctx.obj

    async def _go() -> None:
        backend_obj = _make_detached_backend(state)
        try:
            if not follow:
                typer.echo(await backend_obj.logs(job_id), nl=False)
                return
            offset = 0
            while True:
                text, offset = await backend_obj.log_tail(job_id, offset=offset)
                if text:
                    typer.echo(text, nl=False)
                if (await backend_obj.status(job_id)).state.is_terminal:
                    text, offset = await backend_obj.log_tail(job_id, offset=offset)
                    if text:
                        typer.echo(text, nl=False)
                    break
                await asyncio.sleep(1.0)
        finally:
            await backend_obj.aclose()

    _run(_go())


@job_app.command(name="result")
def job_result(ctx: typer.Context, job_id: str = typer.Argument(...)) -> None:
    """Wait for a detached job to finish and print its result."""
    state: _State = ctx.obj

    async def _go() -> None:
        backend_obj = _make_detached_backend(state)
        try:
            result = await backend_obj.result(job_id)
            _emit(result.stdout, "")
            typer.echo(f"[{result.id}] {result.state.value} exit={result.exit_code}", err=True)
            if not result.ok:
                raise typer.Exit(1)
        finally:
            await backend_obj.aclose()

    _run(_go())


@job_app.command(name="cancel")
def job_cancel(ctx: typer.Context, job_id: str = typer.Argument(...)) -> None:
    """Cancel a running detached job (signals its process group)."""
    state: _State = ctx.obj

    async def _go() -> None:
        backend_obj = _make_detached_backend(state)
        try:
            await backend_obj.cancel(job_id)
            typer.echo(f"cancelled {job_id}")
        finally:
            await backend_obj.aclose()

    _run(_go())


@job_app.command(name="list")
def job_list(ctx: typer.Context) -> None:
    """List detached jobs recorded in the local state store."""
    state: _State = ctx.obj

    async def _go() -> None:
        backend_obj = _make_detached_backend(state)
        try:
            jobs = await backend_obj.list_jobs()
            if not jobs:
                typer.echo("No detached jobs.")
                return
            for info in jobs:
                typer.echo(f"[{info.id}] {info.state.value} {info.accelerator.value}")
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
