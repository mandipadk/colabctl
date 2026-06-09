#!/usr/bin/env python3
"""Live smoke test for the Modal backend (uses ~/.modal.toml auth).

Run: `uv run --extra modal python spikes/modal_smoke.py`
Validates create → exec → read stdout → wait → terminate end-to-end, on CPU
(cheap) and on a T4 (proves the GPU path). Always lets the backend tear down.
"""

from __future__ import annotations

import asyncio

from colabctl.backends import JobSpec, ModalBackend
from colabctl.models import Accelerator


async def main() -> None:
    backend = ModalBackend()
    try:
        print("[CPU] submitting (validates orchestration cheaply) ...", flush=True)
        cpu = await backend.run(
            JobSpec(
                code="import sys; print('hello from modal', sys.version.split()[0])",
                accelerator=Accelerator.NONE,
                timeout=180,
            )
        )
        print(f"[CPU] state={cpu.state} exit={cpu.exit_code} stdout={cpu.stdout!r}", flush=True)
        if cpu.error:
            print(f"[CPU] error: {cpu.error}", flush=True)

        print("[T4] submitting (validates GPU path: nvidia-smi) ...", flush=True)
        gpu_code = (
            "import subprocess\n"
            "out = subprocess.run(['nvidia-smi','--query-gpu=name,memory.total',"
            "'--format=csv,noheader'], capture_output=True, text=True)\n"
            "print('GPU:', out.stdout.strip() or out.stderr.strip() or 'n/a')\n"
        )
        t4 = await backend.run(
            JobSpec(code=gpu_code, accelerator=Accelerator.T4, timeout=240)
        )
        print(f"[T4] state={t4.state} exit={t4.exit_code} stdout={t4.stdout!r}", flush=True)
        if t4.error:
            print(f"[T4] error: {t4.error}", flush=True)
    finally:
        await backend.aclose()


if __name__ == "__main__":
    asyncio.run(main())
