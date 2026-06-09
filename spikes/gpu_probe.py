#!/usr/bin/env python3
"""colabctl Phase 0 — VM probe.

Runs *on the Colab runtime* via `colab exec -f spikes/gpu_probe.py`. It validates
that we actually got a working GPU runtime and records the environment so the spec's
assumptions (accelerator type, CUDA, driver, RAM, disk, idle behavior) can be checked
against reality. All output is plain stdout with stable `KEY=value` markers and clear
BEGIN/END fences so the captured transcript is easy to diff and parse later.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone


def _run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return (out.stdout + out.stderr).strip()
    except Exception as exc:  # noqa: BLE001 - probe must never crash the cell
        return f"<error running {' '.join(cmd)}: {exc!r}>"


def main() -> None:
    print("=== COLABCTL PHASE0 PROBE START ===")
    print(f"PROBE_TIME_UTC={datetime.now(timezone.utc).isoformat()}")
    print(f"PROBE_PY_VERSION={sys.version.split()[0]}")
    print(f"PROBE_PLATFORM={platform.platform()}")
    print(f"PROBE_HOSTNAME={platform.node()}")
    print(f"PROBE_CPU_COUNT={os.cpu_count()}")

    # --- memory / disk -----------------------------------------------------
    try:
        with open("/proc/meminfo") as fh:
            mem_total_kb = int(fh.readline().split()[1])
        print(f"PROBE_RAM_GB={mem_total_kb / 1024 / 1024:.1f}")
    except Exception as exc:  # noqa: BLE001
        print(f"PROBE_RAM_GB=<unknown: {exc!r}>")
    try:
        usage = shutil.disk_usage("/content")
        print(f"PROBE_DISK_GB_TOTAL={usage.total / 1e9:.1f}")
        print(f"PROBE_DISK_GB_FREE={usage.free / 1e9:.1f}")
    except Exception as exc:  # noqa: BLE001
        print(f"PROBE_DISK_GB=<unknown: {exc!r}>")

    # --- accelerator -------------------------------------------------------
    print("--- nvidia-smi ---")
    print(_run(["nvidia-smi"]))
    print("--- nvidia-smi (query) ---")
    print(_run([
        "nvidia-smi",
        "--query-gpu=name,memory.total,driver_version,compute_cap",
        "--format=csv,noheader",
    ]))

    # --- torch / cuda sanity (the real 'can it compute on GPU' check) ------
    try:
        import torch  # noqa: PLC0415 - intentional runtime import on the VM

        print(f"PROBE_TORCH_VERSION={torch.__version__}")
        cuda = torch.cuda.is_available()
        print(f"PROBE_CUDA_AVAILABLE={cuda}")
        if cuda:
            print(f"PROBE_CUDA_DEVICE={torch.cuda.get_device_name(0)}")
            print(f"PROBE_CUDA_VERSION={torch.version.cuda}")
            # actually run a matmul on the GPU to prove it computes
            x = torch.randn(4096, 4096, device="cuda")
            y = torch.randn(4096, 4096, device="cuda")
            z = (x @ y).sum().item()
            torch.cuda.synchronize()
            print(f"PROBE_MATMUL_OK=True PROBE_MATMUL_CHECKSUM={z:.4f}")
            free, total = torch.cuda.mem_get_info()
            print(f"PROBE_VRAM_GB_TOTAL={total / 1e9:.1f} PROBE_VRAM_GB_FREE={free / 1e9:.1f}")
    except Exception as exc:  # noqa: BLE001
        print(f"PROBE_TORCH=<error: {exc!r}>")

    # --- rich-output sanity: does display_data/image round-trip? -----------
    # (kept import-guarded; only meaningful when the kernel forwards rich outputs)
    try:
        import matplotlib  # noqa: PLC0415

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # noqa: PLC0415

        fig = plt.figure()
        plt.plot([0, 1, 2, 3], [0, 1, 4, 9])
        fig.savefig("/content/colabctl_probe_plot.png")
        print("PROBE_PLOT_SAVED=/content/colabctl_probe_plot.png")
    except Exception as exc:  # noqa: BLE001
        print(f"PROBE_PLOT=<skipped: {exc!r}>")

    # --- environment summary as one JSON line (for later machine parsing) --
    summary = {
        "py": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
    }
    print(f"PROBE_JSON={json.dumps(summary)}")
    print("=== COLABCTL PHASE0 PROBE END ===")


if __name__ == "__main__":
    main()
