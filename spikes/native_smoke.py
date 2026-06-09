#!/usr/bin/env python3
"""Live smoke test for the native /tun/m/* transport + the keep-alive fix.

Validates, against a real Colab Pro account (ADC auth):
  1. native runtime allocation via our /tun/m/* client,
  2. code execution over the Jupyter kernel (our recipe + output normalization),
  3. the API-key-only keep-alive (the planned fix for the ADC serviceusage 403),
  4. and — for comparison — reproduces the bearer-auth 403 the CLI hits.

Always tears the runtime down. Run: `uv run --extra native python spikes/native_smoke.py`.
"""

from __future__ import annotations

import asyncio
import traceback

from colabctl.auth import ADCAuthProvider
from colabctl.models import Accelerator, RuntimeSpec
from colabctl.transport.native import NativeColabTransport

NAME = "cc-native-smoke"


async def main() -> None:
    transport = NativeColabTransport.create(ADCAuthProvider())
    endpoint: str | None = None
    try:
        print("[1] allocate T4 via native /tun/m/* ...", flush=True)
        info = await transport.allocate(RuntimeSpec(accelerator=Accelerator.T4, name=NAME))
        endpoint = info.endpoint
        print(
            f"    ALLOCATE OK  name={info.name}  endpoint={info.endpoint}  "
            f"acc={info.accelerator.value}  variant={info.variant.value}",
            flush=True,
        )

        print("[2] execute code on the native kernel ...", flush=True)
        code = (
            "import sys, torch\n"
            "print('PY', sys.version.split()[0])\n"
            "print('CUDA', torch.cuda.is_available(),"
            " torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)\n"
            "print('SUM', sum(range(100)))\n"
        )
        result = await transport.execute(NAME, code, timeout=120)
        print(f"    EXEC ok={result.ok}  status={result.status}", flush=True)
        print("    STDOUT:", repr(result.text[:300]), flush=True)
        if result.error is not None:
            print("    ERROR:", result.error.ename, result.error.evalue, flush=True)

        print("[3] keep-alive (API-key-only — the ADC-403 fix) ...", flush=True)
        try:
            await transport.keep_alive(NAME)
            print("    KEEPALIVE(api-key-only): OK   <-- fix works", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"    KEEPALIVE(api-key-only): FAILED: {exc!r}"[:300], flush=True)

        print("[4] keep-alive WITH bearer (reproduce the CLI's 403) ...", flush=True)
        try:
            await transport._client.keep_alive(endpoint, use_bearer=True)  # noqa: SLF001
            print("    KEEPALIVE(bearer): OK (unexpected — 403 not reproduced)", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"    KEEPALIVE(bearer): FAILED (expected): {exc!r}"[:200], flush=True)
    except Exception:  # noqa: BLE001
        print("SMOKE ERROR:\n" + traceback.format_exc(), flush=True)
    finally:
        print("[5] teardown ...", flush=True)
        try:
            await transport.stop(NAME)
            print("    STOP OK", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"    STOP err: {exc!r}"[:200], flush=True)
        await transport.aclose()


if __name__ == "__main__":
    asyncio.run(main())
