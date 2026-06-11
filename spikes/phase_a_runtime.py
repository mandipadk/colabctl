#!/usr/bin/env python3
"""Phase A — runtime-bundled live probes (one allocation, five design decisions).

Validates, against a real Colab Pro account (ADC auth), the load-bearing unknowns the
1x→10x plan (docs/plan.md) marks as spike-gated — all on a SINGLE T4 to conserve
compute units (the Phase 0 discipline):

  contents    — Jupyter contents REST API reachable through the runtime proxy?
                → decides Pillar 3a (chunked REST transfer vs. the chunked-kernel-exec
                  fallback). The pre-verification review scored the contents-API 2/AVOID
                  *before* the proxy header recipe was verified; this re-tests it empirically.
  kernels     — GET/POST /api/kernels[/{id}/interrupt] reachable through the proxy?
                → decides §5.3 (cancel/interrupt) and §5.6 (ws reconnect by kernel_id).
  refresh     — does re-running the assign GET pre-flight with the SAME nbh return the
                existing runtime with a FRESH proxy token (non-disruptive refresh)?
                → decides §5.10 (refresh_before_expiry vs. disruptive re-assign).
  reconnect   — can we drop the kernel websocket and re-dial the SAME kernel_id with
                state intact (x set before the drop still readable after)?
                → validates §5.6 and the whole "connection is not the data plane" thesis.
  a100        — is this account entitled to an A100 right now (or HTTP 400)?
                → the carried Phase-0 TODO (§A spike ⑦).

Every probe reports PASS / FAIL / UNKNOWN with the raw evidence (status codes, tokens
elided), and the runtime is ALWAYS torn down. Findings go into spikes/PHASE-A-FINDINGS.md.

Run:  uv run --extra native python spikes/phase_a_runtime.py            # all probes
      uv run --extra native python spikes/phase_a_runtime.py contents kernels
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import sys
import tempfile
import traceback
import uuid
from pathlib import Path
from typing import Any

import httpx

from colabctl.auth import ADCAuthProvider
from colabctl.errors import AcceleratorUnavailableError
from colabctl.models import Accelerator, Assignment
from colabctl.transport.native.client import ColabBackendClient
from colabctl.transport.native.contents import ContentsTransfer
from colabctl.transport.native.kernel import NativeKernel

ALL_PROBES = ["contents", "transfer", "kernels", "refresh", "reconnect", "a100"]
_PROBE_FILE = "colabctl_probe.txt"


def _elide(token: str) -> str:
    return f"{token[:6]}…{token[-4:]} (len={len(token)})" if token else "<empty>"


def _base(url: str) -> str:
    return url.rstrip("/")


# --- contents REST API -------------------------------------------------------


async def probe_contents(http: httpx.AsyncClient, a: Assignment) -> dict[str, Any]:
    """Try the Jupyter contents API through the proxy under three auth placements."""
    rpi = a.runtime_proxy_info
    assert rpi is not None
    base = _base(rpi.url)
    hdr = ColabBackendClient.proxy_kernel_headers(rpi.token)
    placements = {
        "header-only": (hdr, {}),
        "header+token-param": (hdr, {"token": rpi.token}),
        "header+proxy-token-param": (hdr, ColabBackendClient.proxy_ws_params(rpi.token)),
    }
    attempts: list[dict[str, Any]] = []
    winner: str | None = None
    for label, (headers, params) in placements.items():
        row: dict[str, Any] = {"placement": label}
        try:
            r = await http.get(f"{base}/api/contents/", headers=headers, params=params or None)
            row["list_status"] = r.status_code
            if r.status_code == 200:
                body = {
                    "type": "file",
                    "format": "text",
                    "content": "hello from colabctl phase-a",
                }
                rp = await http.put(
                    f"{base}/api/contents/{_PROBE_FILE}",
                    headers=headers,
                    params=params or None,
                    json=body,
                )
                row["put_status"] = rp.status_code
                rg = await http.get(
                    f"{base}/api/contents/{_PROBE_FILE}",
                    headers=headers,
                    params=params or None,
                )
                row["get_status"] = rg.status_code
                row["roundtrip_ok"] = (
                    rg.status_code == 200
                    and "hello from colabctl" in rg.text
                )
                if row.get("roundtrip_ok"):
                    winner = label
        except Exception as exc:
            row["error"] = repr(exc)[:200]
        attempts.append(row)
        if winner:
            break
    verdict = "PASS" if winner else "FAIL"
    return {
        "verdict": verdict,
        "winning_placement": winner,
        "attempts": attempts,
        "decides": (
            "Pillar 3a: REST transfer is viable"
            if winner
            else "Pillar 3a: use chunked-kernel-exec fallback"
        ),
    }


# --- chunked upload + ranged download (Pillar 3a) ----------------------------


async def probe_transfer(client: ColabBackendClient, a: Assignment) -> dict[str, Any]:
    """Round-trip a multi-chunk file via ContentsTransfer (chunked PUT + ranged GET)."""
    rpi = a.runtime_proxy_info
    assert rpi is not None
    transfer = ContentsTransfer(client, chunk_size=1 << 20)  # 1 MiB → forces chunking
    payload = os.urandom(3 * (1 << 20) + 12_345)  # ~3 MiB, not a chunk multiple
    digest = hashlib.sha256(payload).hexdigest()
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "up.bin"
        src.write_bytes(payload)
        await transfer.upload(rpi.url, rpi.token, src, "colabctl_xfer.bin")
        dst = Path(d) / "down.bin"
        await transfer.download(rpi.url, rpi.token, "colabctl_xfer.bin", dst)
        got = dst.read_bytes()
    ok = hashlib.sha256(got).hexdigest() == digest and len(got) == len(payload)
    return {
        "verdict": "PASS" if ok else "FAIL",
        "bytes": len(payload),
        "roundtrip_ok": ok,
        "decides": "Pillar 3a: chunked upload + ranged download verified live"
        if ok
        else "Pillar 3a: transfer round-trip mismatch — investigate before relying on chunking",
    }


# --- kernels REST (list / interrupt route reachability) ----------------------


async def probe_kernels(
    http: httpx.AsyncClient, a: Assignment, kernel_id: str | None
) -> dict[str, Any]:
    rpi = a.runtime_proxy_info
    assert rpi is not None
    base = _base(rpi.url)
    hdr = ColabBackendClient.proxy_kernel_headers(rpi.token)
    params = ColabBackendClient.proxy_ws_params(rpi.token)
    out: dict[str, Any] = {}
    try:
        r = await http.get(f"{base}/api/kernels", headers=hdr, params=params)
        out["list_status"] = r.status_code
        out["kernels_seen"] = [k.get("id") for k in r.json()] if r.status_code == 200 else None
    except Exception as exc:
        out["list_error"] = repr(exc)[:200]
    if kernel_id:
        try:
            ri = await http.post(
                f"{base}/api/kernels/{kernel_id}/interrupt", headers=hdr, params=params
            )
            out["interrupt_status"] = ri.status_code  # 204 == route works (idle kernel)
        except Exception as exc:
            out["interrupt_error"] = repr(exc)[:200]
    reachable = out.get("list_status") == 200
    return {
        "verdict": "PASS" if reachable else "FAIL",
        **out,
        "decides": "§5.3 interrupt + §5.6 reconnect are REST-feasible"
        if reachable
        else "interrupt/reconnect need a non-REST path",
    }


# --- same-nbh token refresh --------------------------------------------------


async def probe_refresh(
    client: ColabBackendClient, nb: uuid.UUID, first: Assignment
) -> dict[str, Any]:
    """Re-assign with the SAME notebook id; see if the runtime stays but the token rotates."""
    rpi1 = first.runtime_proxy_info
    assert rpi1 is not None
    second = await client.assign(accelerator=first.accelerator, notebook_id=nb)
    rpi2 = second.runtime_proxy_info
    same_runtime = second.endpoint == first.endpoint
    fresh_token = bool(rpi2 and rpi2.token and rpi2.token != rpi1.token)
    if same_runtime and fresh_token:
        verdict, note = "PASS", "non-disruptive refresh works (same runtime, new token)"
    elif same_runtime:
        verdict, note = "PARTIAL", "reattach works but token did not rotate (reuse stored token)"
    else:
        verdict, note = "FAIL", "same nbh allocated a NEW runtime — no refresh path"
    return {
        "verdict": verdict,
        "same_runtime": same_runtime,
        "fresh_token": fresh_token,
        "endpoint_1": first.endpoint,
        "endpoint_2": second.endpoint,
        "token_1": _elide(rpi1.token),
        "token_2": _elide(rpi2.token) if rpi2 else None,
        "decides": "§5.10: " + note,
    }


# --- websocket reconnect to a surviving kernel -------------------------------


async def probe_reconnect(a: Assignment) -> dict[str, Any]:
    rpi = a.runtime_proxy_info
    assert rpi is not None
    k1 = NativeKernel(rpi.url, rpi.token)
    await k1.start()
    await k1.execute("x = 42; print('SET x')", timeout=60)
    kid = k1._kernel_id
    await k1.stop()  # drop the websocket; server-side kernel survives (_own_kernel=False)
    if not kid:
        return {"verdict": "UNKNOWN", "note": "kernel id not captured; cannot test reconnect"}
    k2 = NativeKernel(rpi.url, rpi.token, kernel_id=kid)
    await k2.start()
    result = await k2.execute("print(x)", timeout=60)
    await k2.stop()
    survived = "42" in result.text
    return {
        "verdict": "PASS" if survived else "FAIL",
        "kernel_id": kid,
        "post_reconnect_stdout": result.text[:120],
        "decides": "§5.6: reconnect-by-kernel_id keeps state"
        if survived
        else "§5.6: reconnect did not preserve state",
    }


# --- A100 entitlement --------------------------------------------------------


async def probe_a100(client: ColabBackendClient) -> dict[str, Any]:
    nb = uuid.uuid4()
    try:
        a = await client.assign(accelerator=Accelerator.A100, notebook_id=nb)
    except AcceleratorUnavailableError as exc:
        return {"verdict": "PASS", "entitled": False, "detail": str(exc)[:200]}
    except Exception as exc:
        return {"verdict": "UNKNOWN", "error": repr(exc)[:200]}
    # Entitled — release immediately to avoid burning A100 units.
    with contextlib.suppress(Exception):
        await client.unassign(a.endpoint)
    return {"verdict": "PASS", "entitled": True, "endpoint": a.endpoint}


# --- driver ------------------------------------------------------------------


async def main(selected: list[str]) -> None:
    auth = ADCAuthProvider()
    http = httpx.AsyncClient(timeout=60.0)
    client = ColabBackendClient(http, token_provider=auth.as_token_callable())
    nb = uuid.uuid4()
    results: dict[str, Any] = {}
    assignment: Assignment | None = None
    kernel_id: str | None = None

    try:
        print(f"[allocate] T4 (notebook_id={nb}) ...", flush=True)
        assignment = await client.assign(accelerator=Accelerator.T4, notebook_id=nb)
        print(f"  endpoint={assignment.endpoint}", flush=True)

        # reconnect first (it leaves a known kernel_id behind for the kernels probe)
        if "reconnect" in selected:
            print("[reconnect] ...", flush=True)
            results["reconnect"] = await probe_reconnect(assignment)
            kernel_id = results["reconnect"].get("kernel_id")

        if "contents" in selected:
            print("[contents] ...", flush=True)
            results["contents"] = await probe_contents(http, assignment)

        if "transfer" in selected:
            print("[transfer] ...", flush=True)
            results["transfer"] = await probe_transfer(client, assignment)

        if "kernels" in selected:
            print("[kernels] ...", flush=True)
            results["kernels"] = await probe_kernels(http, assignment, kernel_id)

        if "refresh" in selected:
            print("[refresh] ...", flush=True)
            results["refresh"] = await probe_refresh(client, nb, assignment)

        if "a100" in selected:
            print("[a100] ...", flush=True)
            results["a100"] = await probe_a100(client)
    except Exception:
        print("PROBE DRIVER ERROR:\n" + traceback.format_exc(), flush=True)
    finally:
        if assignment is not None:
            print("[teardown] unassign ...", flush=True)
            try:
                await client.unassign(assignment.endpoint)
                print("  STOP OK", flush=True)
            except Exception as exc:
                print(f"  STOP err: {exc!r}"[:200], flush=True)
        await http.aclose()

    print("\n===== PHASE-A RUNTIME SUMMARY =====", flush=True)
    for name in selected:
        r = results.get(name, {"verdict": "SKIPPED"})
        print(f"  {name:10s} {r.get('verdict','?'):8s} {r.get('decides','')}", flush=True)
    print("\n----- raw JSON (paste into PHASE-A-FINDINGS.md) -----", flush=True)
    print(json.dumps(results, indent=2, default=str), flush=True)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    chosen = [p for p in (args or ALL_PROBES) if p in ALL_PROBES]
    unknown = [a for a in args if a not in ALL_PROBES]
    if unknown:
        print(f"unknown probe(s): {unknown}; valid: {ALL_PROBES}", file=sys.stderr)
        sys.exit(2)
    asyncio.run(main(chosen))
