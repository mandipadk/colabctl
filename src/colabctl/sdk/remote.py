"""``@remote`` — run a local Python function on a Colab GPU and get the result.

Args, the function, and the return value are marshalled with cloudpickle (base64
over the kernel). The remote harness installs cloudpickle on the VM if absent. The
marshalling/harness helpers are pure and unit-tested; the decorated callable is
usable both synchronously (``f(...)``) and asynchronously (``await f.aio(...)``).

Caveats: the function and its arguments/return value must be cloudpickle-able, and
any third-party imports the function needs must be available (or installed) on the
runtime. For heavy/long jobs prefer an explicit :class:`ColabSession`.

Example::

    @remote(gpu="A100")
    def train():
        import torch
        return torch.cuda.get_device_name(0)

    print(train())            # blocks, runs on an A100, returns the device name
"""

from __future__ import annotations

import asyncio
import base64
import functools
import json
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar, cast

from colabctl.errors import ExecutionError, SerializationError
from colabctl.sdk.client import ColabClient

P = ParamSpec("P")
R = TypeVar("R")

RESULT_BEGIN = "<<<COLABCTL_RESULT>>>"
RESULT_END = "<<<COLABCTL_RESULT_END>>>"


def _load_cloudpickle() -> Any:
    try:
        import cloudpickle
    except ImportError as exc:  # pragma: no cover - only without the extra
        raise SerializationError(
            "cloudpickle is required for @remote. Install with `pip install 'colabctl[sdk]'`."
        ) from exc
    return cloudpickle


# --- pure marshalling helpers (offline-tested) ------------------------------


def encode_call(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """cloudpickle ``(fn, args, kwargs)`` → base64 string."""
    cp = _load_cloudpickle()
    try:
        return base64.b64encode(cp.dumps((fn, args, kwargs))).decode()
    except Exception as exc:
        raise SerializationError(f"Could not serialize the remote call: {exc}") from exc


def build_remote_harness(
    payload_b64: str,
    *,
    requirements: list[str] | None = None,
    env: dict[str, str] | None = None,
    cloudpickle_version: str | None = None,
) -> str:
    """Build the VM-side code that unpickles, runs, and re-pickles the result.

    Optionally injects ``env`` into ``os.environ``, pins ``cloudpickle`` to the host's
    version (so the by-value pickle stays wire-compatible — Colab's base image ships a
    different version, the classic ``@remote`` skew footgun), and ``pip install``s the
    declared ``requirements`` before the call.
    """
    cp_spec = f"cloudpickle=={cloudpickle_version}" if cloudpickle_version else "cloudpickle"
    lines = ["import base64, os, subprocess, sys"]
    if env:
        lines.append(f"os.environ.update({json.dumps(env)})")
    lines += [
        "try:",
        "    import cloudpickle as _cp",
        f"    _need = {json.dumps(cloudpickle_version)}",
        "    if _need and _cp.__version__ != _need:",
        "        raise ImportError('cloudpickle version skew')",
        "except Exception:",
        f"    subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', {json.dumps(cp_spec)}],"
        " check=True)",
        "    import cloudpickle as _cp",
    ]
    if requirements:
        reqs = json.dumps(list(requirements))
        lines.append(
            f"subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', *{reqs}], check=True)"
        )
    lines += [
        f"_payload = base64.b64decode({json.dumps(payload_b64)})",
        "_fn, _args, _kwargs = _cp.loads(_payload)",
        # Catch the user exception and ship it back by value (cloudpickled, with the remote
        # traceback) so the caller re-raises a NATIVE exception locally — not a flattened
        # ename/evalue string. Status-tag the frame 'ok'/'err'.
        "try:",
        "    _status, _blob = 'ok', _cp.dumps(_fn(*_args, **_kwargs))",
        "except Exception as _e:",
        "    import traceback as _tbmod",
        "    try:",
        "        _blob = _cp.dumps({'exc': _e, 'tb': _tbmod.format_exc()})",
        "    except Exception:",  # an unpicklable exception → ship the traceback only
        "        _blob = _cp.dumps({'exc': None, 'tb': _tbmod.format_exc()})",
        "    _status = 'err'",
        "_enc = base64.b64encode(_blob).decode()",
        f"print({json.dumps(RESULT_BEGIN)} + _status + '|' + _enc + {json.dumps(RESULT_END)})",
    ]
    return "\n".join(lines) + "\n"


def parse_result(text: str) -> tuple[str, bytes]:
    """Extract ``(status, payload-bytes)`` framed by the harness markers.

    ``status`` is ``'ok'`` or ``'err'``. A frame with no ``status|`` prefix is treated as
    ``'ok'`` (back-compat with the pre-exception-reraise harness).
    """
    start = text.find(RESULT_BEGIN)
    end = text.find(RESULT_END, start + len(RESULT_BEGIN)) if start != -1 else -1
    if start == -1 or end == -1:
        raise SerializationError("Remote result markers not found in kernel output.")
    inner = text[start + len(RESULT_BEGIN) : end].strip()
    status, sep, encoded = inner.partition("|")
    if not sep:  # legacy frame with no status tag
        status, encoded = "ok", inner
    try:
        return status, base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise SerializationError("Could not decode the remote result payload.") from exc


def parse_result_payload(text: str) -> bytes:
    """The payload bytes of a result frame (back-compat; ignores the status tag)."""
    return parse_result(text)[1]


def decode_result(text: str) -> Any:
    """Decode the harness frame: return the value on success, or **re-raise the remote
    exception locally** (with its remote traceback attached) on failure."""
    cp = _load_cloudpickle()
    status, payload = parse_result(text)
    obj = cp.loads(payload)
    if status == "err":
        exc = obj.get("exc") if isinstance(obj, dict) else None
        tb = obj.get("tb", "") if isinstance(obj, dict) else ""
        if isinstance(exc, BaseException):
            if tb:
                exc.add_note("Remote traceback (on the runtime):\n" + tb)
            raise exc
        raise ExecutionError("Remote execution failed:\n" + (tb or "unknown error"))
    return obj


# --- orchestration ----------------------------------------------------------


async def _run_remote(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    gpu: str,
    transport: str,
    keep: bool,
    client: ColabClient | None,
    timeout: float | None,
    requirements: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> Any:
    own_client = client is None
    cl = client or ColabClient(transport_name=transport)
    cp_version = getattr(_load_cloudpickle(), "__version__", None)
    try:
        session = await cl.allocate(gpu=gpu, keep=keep)
        async with session:
            harness = build_remote_harness(
                encode_call(fn, args, kwargs),
                requirements=requirements,
                env=env,
                cloudpickle_version=cp_version,
            )
            result = await session.run(harness, timeout=timeout)
            if not result.ok:
                err = result.error
                raise ExecutionError(
                    f"Remote execution of {getattr(fn, '__name__', 'function')} failed: "
                    f"{err.ename if err else 'unknown'}: {err.evalue if err else ''}",
                    ename=err.ename if err else None,
                    evalue=err.evalue if err else None,
                    traceback=err.traceback if err else None,
                )
            return decode_result(result.text)
    finally:
        if own_client:
            await cl.aclose()


def remote(
    func: Callable[P, R] | None = None,
    *,
    gpu: str = "T4",
    transport: str = "cli",
    keep: bool = False,
    client: ColabClient | None = None,
    timeout: float | None = None,
    requirements: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> Any:
    """Decorator: run the wrapped function on a Colab GPU.

    Usable as ``@remote`` or ``@remote(gpu="A100", requirements=["torch"], env={...})``.
    ``requirements`` are pip-installed and ``env`` injected on the runtime before the call;
    cloudpickle is pinned to the host's version to avoid the by-value-pickle skew. The
    returned callable runs synchronously by default; call ``.aio(...)`` for the awaitable form.
    """

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        async def awrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            return cast(
                R,
                await _run_remote(
                    fn,
                    args,
                    kwargs,
                    gpu=gpu,
                    transport=transport,
                    keep=keep,
                    client=client,
                    timeout=timeout,
                    requirements=requirements,
                    env=env,
                ),
            )

        @functools.wraps(fn)
        def swrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            return asyncio.run(awrapper(*args, **kwargs))

        # Expose the async form for callers already inside an event loop.
        swrapper.aio = awrapper  # type: ignore[attr-defined]
        return swrapper

    if func is not None:
        return decorator(func)
    return decorator
