#!/usr/bin/env python3
"""Scheduled live canary — catch Google ``/tun/m/*`` protocol drift + end-to-end breakage.

The native transport's dominant risk is Google silently changing the backend. This runs
the real path on a cheap T4 — allocate → fingerprint the raw ``assignments``/``ccu-info``
shapes → execute code → round-trip a file via the contents API → tear down — and a CLI
version/parser probe, then compares the response *shapes* against a committed baseline
(``spikes/canary-baseline.json``). Exit code 0 = healthy, 1 = drift or breakage, so a
scheduled CI job converts "users discover Google broke us" into "the canary told us".

First run with no baseline *establishes* one (commit it). Run:
    COLABCTL_ENABLE_NATIVE=1 uv run --extra native python spikes/canary.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sys
import tempfile
import traceback
import uuid
from pathlib import Path
from typing import Any

import httpx

from colabctl.auth import ADCAuthProvider
from colabctl.drift import skeleton_diff, structural_fingerprint, structural_skeleton
from colabctl.models import Accelerator
from colabctl.transport.native.client import (
    ASSIGNMENTS_PATH,
    COLAB_DOMAIN,
    ColabBackendClient,
)
from colabctl.transport.native.contents import ContentsTransfer
from colabctl.transport.native.kernel import NativeKernel

BASELINE = Path(__file__).parent / "canary-baseline.json"


async def native_canary() -> dict[str, Any]:
    auth = ADCAuthProvider()
    http = httpx.AsyncClient(timeout=60.0)
    client = ColabBackendClient(http, token_provider=auth.as_token_callable())
    endpoint: str | None = None
    try:
        assignment = await client.assign(accelerator=Accelerator.T4, notebook_id=uuid.uuid4())
        endpoint = assignment.endpoint
        rpi = assignment.runtime_proxy_info
        assert rpi is not None

        # Raw response shapes (value-independent) for drift detection.
        raw_assignments = await client._request_json(
            "GET", f"{COLAB_DOMAIN}{ASSIGNMENTS_PATH}"
        )
        raw_ccu = await client.ccu_info()
        skeletons = {
            "assignments": structural_skeleton(raw_assignments),
            "ccu-info": structural_skeleton(raw_ccu) if raw_ccu is not None else None,
        }
        fingerprints = {
            k: (structural_fingerprint(v) if v is not None else None)
            for k, v in skeletons.items()
        }

        # Execute liveness.
        kernel = NativeKernel(rpi.url, rpi.token)
        await kernel.start()
        result = await kernel.execute("print(6 * 7)", timeout=60)
        exec_ok = "42" in result.text
        await kernel.stop()

        # Transfer liveness (contents API round-trip).
        transfer = ContentsTransfer(client)
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "canary.bin"
            src.write_bytes(b"colabctl-canary" * 64)
            await transfer.upload(rpi.url, rpi.token, src, "colabctl_canary.bin")
            dst = Path(d) / "canary.out"
            await transfer.download(rpi.url, rpi.token, "colabctl_canary.bin", dst)
            transfer_ok = dst.read_bytes() == src.read_bytes()

        return {
            "ok": bool(exec_ok and transfer_ok),
            "exec_ok": exec_ok,
            "transfer_ok": transfer_ok,
            "fingerprints": fingerprints,
            "skeletons": skeletons,
        }
    finally:
        if endpoint is not None:
            with contextlib.suppress(Exception):
                await client.unassign(endpoint)
        await http.aclose()


async def cli_canary() -> dict[str, Any]:
    """Cheap CLI-transport health: version present and matches the pinned parser grammar."""
    if shutil.which("colab") is None:
        return {"ok": None, "note": "colab binary not installed; skipped"}
    from colabctl.transport.cli import parser
    from colabctl.transport.cli.adapter import ColabCliTransport

    transport = ColabCliTransport()
    try:
        _, out, _ = await transport._run(["version"])
    except Exception as exc:
        return {"ok": False, "error": repr(exc)[:200]}
    version = parser.parse_version(out)
    return {
        "ok": version == parser.PINNED_CLI_VERSION,
        "version": version,
        "pinned": parser.PINNED_CLI_VERSION,
    }


def check_baseline(native: dict[str, Any]) -> tuple[bool, list[str]]:
    """Compare fingerprints to the committed baseline; establish one on first run."""
    fingerprints = native.get("fingerprints") or {}
    skeletons = native.get("skeletons") or {}
    if not fingerprints:
        return True, ["no fingerprints captured (native probe failed?)"]
    if not BASELINE.exists():
        BASELINE.write_text(
            json.dumps({"fingerprints": fingerprints, "skeletons": skeletons}, indent=2)
        )
        return True, [f"baseline established at {BASELINE.name} — commit it"]
    baseline = json.loads(BASELINE.read_text())
    base_fps = baseline.get("fingerprints", {})
    base_skels = baseline.get("skeletons", {})
    drift: list[str] = []
    for key, fp in fingerprints.items():
        if base_fps.get(key) and fp != base_fps[key]:
            diffs = skeleton_diff(base_skels.get(key), skeletons.get(key))
            drift.append(f"{key} DRIFTED: {diffs}")
    return (not drift), (drift or ["shapes match baseline"])


async def main() -> None:
    results: dict[str, Any] = {}
    try:
        results["native"] = await native_canary()
    except Exception:
        results["native"] = {"ok": False, "error": traceback.format_exc()[-600:]}
    results["cli"] = await cli_canary()

    healthy_baseline, notes = check_baseline(results["native"])
    results["baseline"] = notes

    native_ok = bool(results["native"].get("ok"))
    cli_ok = results["cli"].get("ok")
    overall = native_ok and cli_ok in (True, None) and healthy_baseline

    print(json.dumps(results, indent=2, default=str), flush=True)
    print("\nCANARY", "HEALTHY" if overall else "UNHEALTHY", flush=True)
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
