#!/usr/bin/env python3
"""Phase B — tunnel keep-alive validation (gates flipping native ``keepalive=True``).

colabctl now implements Google's google-colab-cli tunnel keep-alive recipe
(``GET /tun/m/<endpoint>/keep-alive/`` + header ``X-Colab-Tunnel: Google``, ReadTimeout
treated as success — see ``ColabBackendClient.tunnel_keep_alive``). The open question is
empirical: does that ping, with **no kernel activity at all**, actually hold a runtime
past Colab's ~90-minute idle-reclamation window? Until a live run says yes, the native
transport advertises ``Capabilities.keepalive=False``.

This spike assigns a real T4, then loops the tunnel ping every ``--interval`` seconds
**without ever touching the kernel**, listing assignments each cycle to detect
reclamation. It always tears the runtime down.

  --mode tunnel   ping the tunnel every INTERVAL (the thing we're validating)
  --mode silent   never ping — the baseline: how soon does an idle runtime get reclaimed?

PASS (mode=tunnel) = the runtime survived the full window with only tunnel pings → it is
safe to flip native ``Capabilities.keepalive=True``. Run silent first (shorter, ~once it
reclaims) if you want the baseline reclamation time on your account.

Prereqs: an ADC login with the colaboratory scope (``colabctl auth login``). No
``COLABCTL_ENABLE_NATIVE`` needed — this drives the low-level client directly.

Run (this is LONG — ~100 minutes for a conclusive tunnel run; defaults: 60s/100min):
    uv run --extra native python spikes/phase_b_keepalive.py --mode tunnel
    uv run --extra native python spikes/phase_b_keepalive.py --mode silent --max-minutes 120
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
import traceback
import uuid
from typing import Any

import httpx

from colabctl.auth import ADCAuthProvider
from colabctl.models import Accelerator
from colabctl.transport.native.client import ColabBackendClient


async def probe(mode: str, interval: float, max_minutes: float) -> dict[str, Any]:
    auth = ADCAuthProvider()
    http = httpx.AsyncClient(timeout=60.0)
    client = ColabBackendClient(http, token_provider=auth.as_token_callable())
    nb = uuid.uuid4()
    timeline: list[dict[str, Any]] = []
    started = time.monotonic()
    endpoint: str | None = None
    try:
        a = await client.assign(accelerator=Accelerator.T4, notebook_id=nb)
        endpoint = a.endpoint
        print(
            f"  assigned endpoint={endpoint}; mode={mode}; "
            f"pinging every {interval:.0f}s with NO kernel activity",
            flush=True,
        )
        deadline = started + max_minutes * 60
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            elapsed = round((time.monotonic() - started) / 60, 1)
            event: dict[str, Any] = {"minutes": elapsed}
            if mode == "tunnel":
                try:
                    await client.tunnel_keep_alive(endpoint)
                    event["pinged"] = True
                except Exception as exc:  # record and keep probing
                    event["ping_error"] = repr(exc)[:200]
            alive = any(s.endpoint == endpoint for s in await client.list_assignments())
            event["assignment_listed"] = alive
            timeline.append(event)
            print(f"  t+{elapsed:>5}min listed={alive} mode={mode}", flush=True)
            if not alive:
                event["reclaimed"] = True
                break
        reclaimed_at = next((e["minutes"] for e in timeline if e.get("reclaimed")), None)
        survived = reclaimed_at is None
        decides = (
            "FLIP native Capabilities.keepalive=True — the tunnel ping held the runtime past idle"
            if survived and mode == "tunnel"
            else "Keep keepalive=False — the tunnel ping did NOT hold the runtime (see timeline)"
            if mode == "tunnel"
            else f"baseline: an idle runtime is reclaimed at ~{reclaimed_at} min on this account"
        )
        return {
            "verdict": "PASS" if survived else "FAIL",
            "mode": mode,
            "interval_s": interval,
            "reclaimed_at_minutes": reclaimed_at,
            "survived_full_window": survived,
            "decides": decides,
            "timeline": timeline,
        }
    finally:
        if endpoint is not None:
            with contextlib.suppress(Exception):
                await client.unassign(endpoint)
        await http.aclose()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["tunnel", "silent"], default="tunnel")
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between tunnel pings")
    ap.add_argument("--max-minutes", type=float, default=100.0, help="give up after this long")
    args = ap.parse_args()
    try:
        result = await probe(args.mode, args.interval, args.max_minutes)
    except Exception:
        print("KEEP-ALIVE PROBE ERROR:\n" + traceback.format_exc(), flush=True)
        return
    print("\n===== PHASE-B TUNNEL KEEP-ALIVE SUMMARY =====", flush=True)
    print(json.dumps(result, indent=2, default=str), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
