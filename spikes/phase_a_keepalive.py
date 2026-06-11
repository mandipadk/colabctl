#!/usr/bin/env python3
"""Phase A — keep-alive probes: cookie/SAPISIDHASH auth + the idle-window measurement.

Two modes (owner decision D2: both keep-alive tracks are developed properly):

  cookie  — Track B. Attempt the RuntimeService KeepAliveAssignment RPC with browser
            **session-cookie** auth (SAPISIDHASH), the one principal Phase 0 found can
            actually call it. This is the live header/cookie-matrix deliverable for the
            SAPISIDHASH recipe. Requires an exported cookie jar (gray-area, opt-in):
                COLABCTL_COOKIE_FILE=/path/to/cookies.{json,txt}
            Cookies are read once, used only for the keep-alive call, never persisted.

  idle    — Spike ⑥. Measure how long a runtime survives, with vs. without periodic
            kernel-activity pings — the unverified "90-min idle window" gap named in
            PHASE0-FINDINGS.md §2. LONG-running (up to ~90+ min); always tears down.
                --mode activity   ping the kernel every INTERVAL (default)
                --mode silent     never ping (baseline reclamation time)

Run:  COLABCTL_COOKIE_FILE=cookies.json \
        uv run --extra native python spikes/phase_a_keepalive.py cookie
      uv run --extra native python spikes/phase_a_keepalive.py idle --mode activity --interval 300
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

import httpx

from colabctl.auth import ADCAuthProvider
from colabctl.models import Accelerator
from colabctl.transport.native.client import (
    COLAB_API_DOMAIN,
    KEEPALIVE_RPC,
    PUBLIC_API_KEY,
    PUBLIC_API_KEY_HEADER,
    USER_PROJECT_HEADER,
    ColabBackendClient,
)
from colabctl.transport.native.client import (
    COLAB_PROJECT_ID as _COLAB_PROJECT,
)
from colabctl.transport.native.kernel import NativeKernel

_ORIGIN = "https://colab.research.google.com"
#: Cookies Google uses to derive the SAPISIDHASH family (first present one wins).
_SAPISID_NAMES = ("SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID")


# --- cookie loading ----------------------------------------------------------


def load_cookies(path: Path) -> dict[str, str]:
    """Load cookies from a flat JSON map, a browser-extension JSON list, or cookies.txt."""
    text = path.read_text()
    if path.suffix == ".json" or text.lstrip().startswith(("{", "[")):
        data = json.loads(text)
        if isinstance(data, dict):
            inner = data.get("cookies", data)
            if isinstance(inner, dict):
                return {str(k): str(v) for k, v in inner.items()}
            data = inner
        if isinstance(data, list):  # [{name, value}, ...]
            return {str(c["name"]): str(c["value"]) for c in data if "name" in c}
        raise ValueError("unrecognized JSON cookie shape")
    # Netscape cookies.txt
    jar: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            jar[parts[5]] = parts[6]
    return jar


def sapisidhash(
    sapisid: str, *, ts: int, origin: str = _ORIGIN, prefix: str = "SAPISIDHASH"
) -> str:
    raw = f"{ts} {sapisid} {origin}".encode()
    return f"{prefix} {ts}_{hashlib.sha1(raw).hexdigest()}"


# --- cookie keep-alive probe -------------------------------------------------


async def probe_cookie(endpoint: str, jar: dict[str, str]) -> dict[str, Any]:
    sapisid = next((jar[n] for n in _SAPISID_NAMES if n in jar), None)
    if sapisid is None:
        return {"verdict": "UNKNOWN", "note": f"no SAPISID cookie found; have {sorted(jar)[:8]}"}
    ts = int(time.time())
    # Google accepts a space-separated set covering 1P/3P principals.
    auth = " ".join(
        sapisidhash(sapisid, ts=ts, prefix=p)
        for p in ("SAPISIDHASH", "SAPISID1PHASH", "SAPISID3PHASH")
    )
    headers = {
        "Content-Type": "application/json+protobuf",
        "Authorization": auth,
        "Origin": _ORIGIN,
        "x-user-agent": "grpc-web-javascript/0.1",
        "x-goog-api-client": "grpc-web/0.1",
        PUBLIC_API_KEY_HEADER: PUBLIC_API_KEY,
        USER_PROJECT_HEADER: _COLAB_PROJECT,
    }
    cookie_header = "; ".join(f"{k}={v}" for k, v in jar.items())
    url = f"{COLAB_API_DOMAIN}{KEEPALIVE_RPC}"
    async with httpx.AsyncClient(timeout=30.0) as http:
        r = await http.post(url, headers={**headers, "Cookie": cookie_header}, json=[endpoint])
    return {
        "verdict": "PASS" if r.is_success else "FAIL",
        "status": r.status_code,
        "body": r.text[:300],
        "sapisid_cookie_used": next(n for n in _SAPISID_NAMES if n in jar),
        "decides": "Track B: SAPISIDHASH keep-alive works"
        if r.is_success
        else "Track B: refine header/cookie matrix (see body)",
    }


# --- idle-window measurement -------------------------------------------------


async def probe_idle(mode: str, interval: float, max_minutes: float) -> dict[str, Any]:
    auth = ADCAuthProvider()
    http = httpx.AsyncClient(timeout=60.0)
    client = ColabBackendClient(http, token_provider=auth.as_token_callable())
    nb = uuid.uuid4()
    timeline: list[dict[str, Any]] = []
    started = time.monotonic()
    endpoint: str | None = None
    kernel: NativeKernel | None = None
    try:
        a = await client.assign(accelerator=Accelerator.T4, notebook_id=nb)
        endpoint = a.endpoint
        rpi = a.runtime_proxy_info
        assert rpi is not None
        kernel = NativeKernel(rpi.url, rpi.token)
        await kernel.start()
        deadline = started + max_minutes * 60
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            elapsed = round((time.monotonic() - started) / 60, 1)
            alive = any(s.endpoint == endpoint for s in await client.list_assignments())
            event: dict[str, Any] = {"minutes": elapsed, "assignment_listed": alive}
            if mode == "activity":
                try:
                    await kernel.execute("None", timeout=30)
                    event["pinged"] = True
                except Exception as exc:
                    event["ping_error"] = repr(exc)[:160]
            timeline.append(event)
            print(f"  t+{elapsed:>5}min listed={alive} mode={mode}", flush=True)
            if not alive:
                event["reclaimed"] = True
                break
        reclaimed_at = next((e["minutes"] for e in timeline if e.get("reclaimed")), None)
        return {
            "verdict": "PASS" if timeline else "UNKNOWN",
            "mode": mode,
            "interval_s": interval,
            "reclaimed_at_minutes": reclaimed_at,
            "survived_full_window": reclaimed_at is None,
            "timeline": timeline,
        }
    finally:
        if kernel is not None:
            with contextlib.suppress(Exception):
                await kernel.stop()
        if endpoint is not None:
            with contextlib.suppress(Exception):
                await client.unassign(endpoint)
        await http.aclose()


# --- driver ------------------------------------------------------------------


async def run_cookie() -> dict[str, Any]:
    cookie_file = os.environ.get("COLABCTL_COOKIE_FILE")
    if not cookie_file:
        return {"verdict": "SKIPPED", "note": "set COLABCTL_COOKIE_FILE to an exported cookie jar"}
    jar = load_cookies(Path(cookie_file))
    auth = ADCAuthProvider()
    http = httpx.AsyncClient(timeout=60.0)
    client = ColabBackendClient(http, token_provider=auth.as_token_callable())
    endpoint: str | None = None
    try:
        a = await client.assign(accelerator=Accelerator.T4, notebook_id=uuid.uuid4())
        endpoint = a.endpoint
        print(f"  endpoint={endpoint}; attempting SAPISIDHASH keep-alive ...", flush=True)
        return await probe_cookie(endpoint, jar)
    finally:
        if endpoint is not None:
            with contextlib.suppress(Exception):
                await client.unassign(endpoint)
        await http.aclose()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["cookie", "idle"])
    ap.add_argument("--mode", dest="idle_mode", choices=["activity", "silent"], default="activity")
    ap.add_argument("--interval", type=float, default=300.0, help="seconds between checks")
    ap.add_argument("--max-minutes", type=float, default=120.0, help="give up after this long")
    args = ap.parse_args()
    try:
        if args.mode == "cookie":
            result = await run_cookie()
        else:
            result = await probe_idle(args.idle_mode, args.interval, args.max_minutes)
    except Exception:
        print("KEEPALIVE PROBE ERROR:\n" + traceback.format_exc(), flush=True)
        return
    print("\n===== PHASE-A KEEP-ALIVE SUMMARY =====", flush=True)
    print(json.dumps(result, indent=2, default=str), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
