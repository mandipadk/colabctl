#!/usr/bin/env python3
"""Check the custom Colab transport for protocol drift and end-to-end breakage.

The scheduled check allocates a low-cost T4 runtime, fingerprints the raw assignments
and ccu-info response shapes, executes code, round-trips a file through the Contents API,
and tears the runtime down.

Response shapes are compared with the reviewed fixture under tests/fixtures. Exit code 0
means healthy; exit code 1 means the protocol shape changed or an end-to-end operation failed.

Run manually with:

    COLABCTL_ENABLE_NATIVE=1 uv run --extra native python .github/canary/run.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import traceback
import uuid
from pathlib import Path
from typing import Any

import httpx

from colabctl.auth import ADCAuthProvider
from colabctl.drift import compare_structural_snapshot, structural_snapshot
from colabctl.models import Accelerator
from colabctl.transport.native.client import (
    ASSIGNMENTS_PATH,
    COLAB_DOMAIN,
    ColabBackendClient,
)
from colabctl.transport.native.contents import ContentsTransfer
from colabctl.transport.native.kernel import NativeKernel

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = REPO_ROOT / "tests" / "fixtures" / "colab_protocol_baseline.json"


async def native_canary() -> dict[str, Any]:
    """Exercise the custom transport and capture its provider response shapes."""
    auth = ADCAuthProvider()
    http = httpx.AsyncClient(timeout=60.0)
    client = ColabBackendClient(http, token_provider=auth.as_token_callable())
    endpoint: str | None = None
    try:
        assignment = await client.assign(accelerator=Accelerator.T4, notebook_id=uuid.uuid4())
        endpoint = assignment.endpoint
        rpi = assignment.runtime_proxy_info
        assert rpi is not None

        raw_assignments = await client._request_json("GET", f"{COLAB_DOMAIN}{ASSIGNMENTS_PATH}")
        raw_ccu = await client.ccu_info()
        fingerprints, skeletons = structural_snapshot(
            {"assignments": raw_assignments, "ccu-info": raw_ccu}
        )

        kernel = NativeKernel(rpi.url, rpi.token)
        await kernel.start()
        result = await kernel.execute("print(6 * 7)", timeout=60)
        exec_ok = "42" in result.text
        await kernel.stop()

        transfer = ContentsTransfer(client)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "canary.bin"
            source.write_bytes(b"colabctl-canary" * 64)
            await transfer.upload(rpi.url, rpi.token, source, "colabctl_canary.bin")
            destination = Path(directory) / "canary.out"
            await transfer.download(
                rpi.url,
                rpi.token,
                "colabctl_canary.bin",
                destination,
            )
            transfer_ok = destination.read_bytes() == source.read_bytes()

        return {
            "ok": bool(exec_ok and transfer_ok),
            "exec_ok": exec_ok,
            "transfer_ok": transfer_ok,
            "fingerprints": fingerprints,
            "skeletons": skeletons,
        }
    finally:
        try:
            if endpoint is not None:
                await client.unassign(endpoint)
        finally:
            await http.aclose()


def check_baseline(native: dict[str, Any]) -> tuple[bool, list[str]]:
    """Compare captured fingerprints with the reviewed baseline."""
    fingerprints = native.get("fingerprints") or {}
    skeletons = native.get("skeletons") or {}
    if not BASELINE.exists():
        return compare_structural_snapshot(fingerprints, skeletons, None)
    return compare_structural_snapshot(
        fingerprints,
        skeletons,
        json.loads(BASELINE.read_text()),
    )


async def main() -> None:
    """Run the compatibility checks and exit nonzero on drift or breakage."""
    results: dict[str, Any] = {}
    try:
        results["native"] = await native_canary()
    except Exception:
        results["native"] = {"ok": False, "error": traceback.format_exc()[-600:]}

    healthy_baseline, notes = check_baseline(results["native"])
    results["baseline"] = notes

    native_ok = bool(results["native"].get("ok"))
    overall = native_ok and healthy_baseline

    print(json.dumps(results, indent=2, default=str), flush=True)
    print("\nCANARY", "HEALTHY" if overall else "UNHEALTHY", flush=True)
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
