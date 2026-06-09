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


def build_remote_harness(payload_b64: str) -> str:
    """Build the VM-side code that unpickles, runs, and re-pickles the result."""
    return (
        "import base64\n"
        "try:\n"
        "    import cloudpickle as _cp\n"
        "except Exception:\n"
        "    import subprocess, sys\n"
        "    subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', 'cloudpickle'],"
        " check=True)\n"
        "    import cloudpickle as _cp\n"
        f"_payload = base64.b64decode({json.dumps(payload_b64)})\n"
        "_fn, _args, _kwargs = _cp.loads(_payload)\n"
        "_result = _fn(*_args, **_kwargs)\n"
        "_enc = base64.b64encode(_cp.dumps(_result)).decode()\n"
        f"print({json.dumps(RESULT_BEGIN)} + _enc + {json.dumps(RESULT_END)})\n"
    )


def parse_result_payload(text: str) -> bytes:
    """Extract the base64 result framed by the harness markers."""
    start = text.find(RESULT_BEGIN)
    end = text.find(RESULT_END, start + len(RESULT_BEGIN)) if start != -1 else -1
    if start == -1 or end == -1:
        raise SerializationError("Remote result markers not found in kernel output.")
    encoded = text[start + len(RESULT_BEGIN) : end].strip()
    try:
        return base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise SerializationError("Could not decode the remote result payload.") from exc


def decode_result(text: str) -> Any:
    """Decode the marker-framed cloudpickle result from kernel output."""
    cp = _load_cloudpickle()
    return cp.loads(parse_result_payload(text))


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
) -> Any:
    own_client = client is None
    cl = client or ColabClient(transport_name=transport)
    try:
        session = await cl.allocate(gpu=gpu, keep=keep)
        async with session:
            harness = build_remote_harness(encode_call(fn, args, kwargs))
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
) -> Any:
    """Decorator: run the wrapped function on a Colab GPU.

    Usable as ``@remote`` or ``@remote(gpu="A100")``. The returned callable runs
    synchronously by default; call ``.aio(...)`` for the awaitable form.
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
