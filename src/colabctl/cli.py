"""``colabctl`` command-line interface (Typer), built on the SDK.

Every command runs through the same :class:`ColabClient` the SDK exposes, so the
CLI and library behave identically. ``--transport`` chooses ``cli`` (sanctioned
default) or ``native`` (opt-in). The client factory is module-level so tests can
inject a fake transport.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
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


@app.command()
def cost(
    gpu: str = typer.Option("A100", "--gpu", help="Accelerator to price (T4/L4/A100/H100)"),
    spot: bool = typer.Option(False, "--spot", help="Show interruptible/spot rates instead"),
    allow: str | None = typer.Option(
        None, "--allow", help="Restrict to these backends (comma-separated)"
    ),
    live: bool = typer.Option(
        False, "--live", help="Pull fresh prices from the live market feed (ComputePrices)"
    ),
) -> None:
    """Estimate GPU cost per backend, cheapest first — the dry-run price view.

    Defaults to the offline static table (deterministic, the trusted routing floor); ``--live``
    overlays the cached, plausibility-guarded market feed. Never launches anything; prices are
    ranking estimates, not binding quotes.
    """
    from colabctl.cost import default_catalog

    accel = _resolve_accelerator(gpu, None, default=Accelerator.A100)
    backends = [n.strip() for n in allow.split(",") if n.strip()] if allow else None

    async def _go() -> None:
        rows = await default_catalog(live=live).per_backend(accel, spot=spot, backends=backends)
        tier = "spot" if spot else "on-demand"
        if not rows:
            typer.echo(f"No {tier} price data for {accel.value}.")
            return
        typer.echo(f"{accel.value} {tier} estimate ($/hr, cheapest first):")
        for r in rows:
            typer.echo(f"  {r.provider:<8} ${r.rate(spot=spot):>6.2f}/hr  [{r.source}]")

    _run(_go())


@app.command()
def spend(
    days: int | None = typer.Option(
        None, "--days", help="Only count spend in the last N days (default: all time)"
    ),
) -> None:
    """Show the cross-backend USD spend ledger (estimated)."""
    from datetime import timedelta

    from colabctl.state import StateStore, utcnow

    store = StateStore()
    since = (utcnow() - timedelta(days=days)) if days else None
    total = store.total_spend_usd(since=since)
    records = [r for r in store.list_spend() if since is None or r.at >= since]
    window = f" (last {days}d)" if days else ""
    typer.echo(f"estimated spend{window}: ${total:.2f} across {len(records)} allocation(s)")
    for r in records[-10:]:
        ts = r.at.strftime("%Y-%m-%d %H:%M")
        typer.echo(f"  {ts}  {r.backend:<8} {r.accelerator.value:<5} ${r.est_cost_usd:>6.2f}")


@app.command(name="spot-risk")
def spot_risk(
    gpu: str | None = typer.Option(None, "--gpu", help="One accelerator (default: all)"),
) -> None:
    """Spot interruption-rate + savings per accelerator (AWS EC2 reference, directional).

    Helps decide whether a GPU's spot tier is worth it: prefer high savings among accelerators
    whose interruption bucket is low. AWS-specific, so treat it as a directional reference for
    colabctl's own spot backends (RunPod/Vast), not a per-backend guarantee.
    """
    from colabctl.cost.risk import SpotRiskSource

    accel = _resolve_accelerator(gpu, None, default=Accelerator.A100) if gpu else None

    async def _go() -> None:
        rows = await SpotRiskSource().risk(accelerator=accel)
        if not rows:
            typer.echo("No spot-risk data (feed unreachable).")
            return
        typer.echo("spot interruption / savings (AWS EC2 reference, directional):")
        for r in rows:
            typer.echo(
                f"  {r.accelerator.value:<5} interruption {r.range_label:<7}  "
                f"savings ~{r.savings_pct}%  (n={r.samples})"
            )

    _run(_go())


def _latest_pypi_version() -> str | None:
    """The latest colabctl version on PyPI (None if unreachable). Patched in tests."""
    import urllib.request

    try:
        with urllib.request.urlopen("https://pypi.org/pypi/colabctl/json", timeout=10) as resp:
            return str(json.load(resp)["info"]["version"])
    except Exception:
        return None


def _upgrade_command(method: str) -> list[str]:
    """The upgrade command for the detected (or chosen) installer: pip or uv-tool."""
    sp, exe = sys.prefix.replace("\\", "/"), sys.executable.replace("\\", "/")
    use_uv = method == "uv" or (
        method == "auto"
        and shutil.which("uv") is not None
        and ("uv/tools" in sp or "uv/tools" in exe)  # a uv-tool-managed venv
    )
    if use_uv:
        return ["uv", "tool", "upgrade", "colabctl"]
    return [sys.executable, "-m", "pip", "install", "--upgrade", "colabctl"]


@app.command()
def update(
    check: bool = typer.Option(
        False, "--check", help="Only report whether a newer version exists; don't upgrade"
    ),
    method: str = typer.Option(
        "auto", "--method", help="Upgrade via: auto (detect) | pip | uv (uv tool)"
    ),
) -> None:
    """Upgrade colabctl to the latest version on PyPI."""
    current = __version__
    latest = _latest_pypi_version()
    typer.echo(f"installed: {current}")
    typer.echo(f"latest:    {latest or '(could not reach PyPI)'}")
    if latest is not None and latest == current:
        typer.echo("colabctl is up to date.")
        return
    if check:
        if latest is not None:
            typer.echo("a newer version is available; run `colabctl update` to upgrade.")
        return
    if latest is None:
        typer.secho("could not determine the latest version (PyPI unreachable).", err=True)
        raise typer.Exit(1)
    cmd = _upgrade_command(method)
    typer.echo("running: " + " ".join(cmd), err=True)
    raise typer.Exit(subprocess.call(cmd))


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

nb_app = typer.Typer(
    name="notebook",
    help="Run parameterized notebooks on a remote GPU (papermill-style).",
    no_args_is_help=True,
)
app.add_typer(nb_app, name="notebook")


def _parse_params(items: list[str]) -> dict[str, Any]:
    """Parse ``KEY=VALUE`` params; VALUE is JSON-decoded when possible (typed), else a string."""
    params: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise typer.BadParameter(f"--param must be KEY=VALUE, got {item!r}")
        key, raw = item.split("=", 1)
        try:
            params[key] = json.loads(raw)
        except json.JSONDecodeError:
            params[key] = raw
    return params


@nb_app.command("run")
def notebook_run(
    ctx: typer.Context,
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Local .ipynb to run"),
    param: list[str] = typer.Option([], "--param", "-p", help="KEY=VALUE parameter (repeatable)"),
    gpu: str = typer.Option("T4", "--gpu", help="Accelerator, or a ladder like 'A100,L4,T4'"),
    detach: bool = typer.Option(False, "--detach", "-d", help="Submit as a durable detached job"),
    out: Path | None = typer.Option(None, "--out", help="Write the executed .ipynb here"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the spend guard"),
) -> None:
    """Inject parameters and run a notebook on a remote GPU (cell-by-cell, or --detach)."""
    from colabctl.notebook import executed_notebook, load_notebook, notebook_to_script, run_notebook

    state: _State = ctx.obj
    params = _parse_params(param)

    if detach:
        accel = _resolve_accelerator(gpu, None, default=Accelerator.T4)
        script = notebook_to_script(load_notebook(file), params)

        async def _go_detached() -> None:
            backend_obj = _make_detached_backend(state)
            try:
                info = await backend_obj.submit(JobSpec(code=script, accelerator=accel))
                typer.echo(info.id)
                typer.echo(
                    f"[{info.id}] submitted; follow with `colabctl job logs -f {info.id}`", err=True
                )
            finally:
                await backend_obj.aclose()

        _run(_go_detached())
        return

    async def _go() -> None:
        async with _make_client(state) as client:
            await _spend_guard(client, gpu, yes)
            session = await client.allocate(gpu=gpu)
            async with session:
                results = await run_notebook(session, file, parameters=params)
                for result in results:
                    _emit(result.text, result.stderr)
                if out is not None:
                    nb = executed_notebook(load_notebook(file), results, parameters=params)
                    out.write_text(json.dumps(nb, indent=1))
                    typer.echo(f"wrote executed notebook: {out}", err=True)
                failed = sum(1 for r in results if not r.ok)
                typer.echo(f"ran {len(results)} cell(s), {failed} failed", err=True)
                if failed:
                    raise typer.Exit(1)

    _run(_go())


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


async def _record_run_spend(
    result: JobResult, accelerator: Accelerator, *, spot: bool, hours: float
) -> None:
    """Append an estimated ``SpendRecord`` for a completed router run (best-effort).

    Closes the cost loop: ``job run`` → the ledger → ``colabctl spend`` and the cumulative
    budget cap. Estimates ``rate * wall-clock-hours`` from the catalog price of the backend
    that actually ran; never raises (ledger bookkeeping must not break a finished run).
    """
    try:
        from colabctl.cost import PriceCatalog
        from colabctl.state import SpendRecord, StateStore

        price = await PriceCatalog().cheapest(accelerator, spot=spot, backends=[result.backend])
        if price is None:
            return
        est = price.rate(spot=spot) * max(hours, 1.0 / 3600.0)
        StateStore().record_spend(
            SpendRecord(
                backend=result.backend, accelerator=accelerator, est_cost_usd=est, hours=hours
            )
        )
    except Exception:
        pass


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
    spot: bool = typer.Option(
        False,
        "--spot",
        help="Prefer the cheaper interruptible/spot tier where a backend offers one",
    ),
    cheapest: bool = typer.Option(
        False, "--cheapest", help="Route to the cheapest capable backend (use with --allow)"
    ),
    max_price: float | None = typer.Option(
        None, "--max-price", help="Refuse any backend pricier than this $/hr (fail-closed cap)"
    ),
    budget: float | None = typer.Option(
        None,
        "--budget",
        help="Refuse to launch if cumulative ledger spend + this run would exceed $N "
        "(fail-closed cumulative cap)",
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
        spot=spot,
        max_price_usd_hr=max_price,
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

    # The cost-routing path (cheapest-first / fail-closed cap) needs the router too.
    use_router = bool(allow) or cheapest or max_price is not None or budget is not None

    async def _go() -> None:
        if use_router:
            names = [n.strip() for n in allow.split(",") if n.strip()] if allow else [backend]
            router = _make_router(names, state)
            try:
                # Fail-closed cumulative budget gate BEFORE launch: project the cheapest
                # eligible candidate's rate (1h) on top of the persisted ledger spend, and
                # refuse if it would breach --budget. Reads the durable ledger so a restart
                # can't reset cumulative spend and slip past the cap.
                if budget is not None:
                    from colabctl.allocation import AllocationGate
                    from colabctl.state import StateStore

                    ranked = await router.cost_ranked(
                        spec, prefer=backend, spot=spot, max_price_usd_hr=max_price
                    )
                    cheapest_row = ranked[0][1] if ranked and ranked[0][1] is not None else None
                    rate = cheapest_row.rate(spot=spot) if cheapest_row is not None else 0.0
                    AllocationGate(budget_usd=budget).authorize(
                        rate_usd_hr=rate,
                        spent_usd=StateStore().total_spend_usd(),
                        max_price_usd_hr=max_price,
                        what="job run",
                    )
                # --backend is the preferred first candidate; fail over to the rest on
                # infra errors (a ran-but-failed user job is never retried elsewhere).
                # With --cheapest/--max-price the candidate order becomes cost order and
                # over-cap backends are refused fail-closed.
                t0 = time.monotonic()
                result = await router.run(
                    spec,
                    prefer=backend,
                    fallback=True,
                    cheapest=cheapest,
                    spot=spot,
                    max_price_usd_hr=max_price,
                )
                await _record_run_spend(
                    result, accelerator, spot=spot, hours=(time.monotonic() - t0) / 3600.0
                )
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


@job_app.command(name="history")
def job_history(ctx: typer.Context, job_id: str = typer.Argument(...)) -> None:
    """Show a detached job's state-transition timeline (when/why each change, incarnation)."""
    from colabctl.state import StateStore

    record = StateStore().get_job(job_id)
    if record is None:
        typer.secho(f"error: no such job: {job_id!r}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if not record.events:
        typer.echo(f"[{record.id}] no recorded transitions (state: {record.state.value})")
        return
    for ev in record.events:
        ts = ev.at.strftime("%Y-%m-%d %H:%M:%S")
        reason = f"  — {ev.reason}" if ev.reason else ""
        typer.echo(
            f"{ts}  inc{ev.incarnation}  {ev.from_state.value} -> {ev.to_state.value}{reason}"
        )


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


@job_app.command(name="gc")
def job_gc(
    ctx: typer.Context,
    ttl_hours: float = typer.Option(
        168.0, "--ttl-hours", help="Prune terminal job records older than this (default 7d)"
    ),
    reconcile: bool = typer.Option(
        True,
        "--reconcile/--no-reconcile",
        help="Mark non-resumable jobs whose runtime is gone as failed",
    ),
) -> None:
    """Reconcile job records against live runtimes and prune stale terminal records."""
    state: _State = ctx.obj

    async def _go() -> None:
        backend_obj = _make_detached_backend(state)
        try:
            report = await backend_obj.gc_jobs(ttl_hours=ttl_hours, reconcile=reconcile)
            for jid in report.reconciled:
                typer.echo(f"  reconciled {jid} -> FAILED (runtime gone)")
            for jid in report.pruned:
                typer.echo(f"  pruned terminal record {jid}")
            typer.echo(f"gc: {len(report.reconciled)} reconciled, {len(report.pruned)} pruned")
        finally:
            await backend_obj.aclose()

    _run(_go())


@job_app.command(name="rm")
def job_rm(ctx: typer.Context, job_id: str = typer.Argument(...)) -> None:
    """Delete a single job record from the local store (does not touch the runtime)."""
    from colabctl.state import StateStore

    if StateStore().delete_job(job_id):
        typer.echo(f"removed {job_id}")
    else:
        typer.secho(f"error: no such job: {job_id!r}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


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
