# Phase A — Findings

**Date:** 2026-06-09 · **Account:** _(redacted Colab Pro)_ · **Verdict: 🟢 GO — clean sweep**

All five runtime-bundled probes PASSED on a single T4 allocation
(`notebook_id=REDACTED-notebook-uuid`,
`endpoint=gpu-t4-s-REDACTED`), torn down cleanly. Every spike-gated
design fork in `docs/plan.md` resolved the favorable way: **no fallbacks needed**, and
the durability thesis (connection ≠ data plane) is empirically validated. The
keep-alive probes (⑥ cookie, ⑦ idle) and ⑧ sidecar capture remain to be run; they size
keep-alive effort but do not gate Pillars 1–3.

---

## ① Contents REST API through the proxy → Pillar 3a ✅ PASS

- **Winning auth placement:** **header-only** (the verified proxy header recipe alone —
  no token query param needed). `GET /api/contents/` → 200, `PUT` → 201, `GET` → 200,
  round-trip body intact.
- **Decision:** **Build Pillar 3a on the Jupyter contents REST API.** The
  chunked-kernel-exec fallback is NOT needed (kept in the plan only as a contingency
  that did not fire). This overturns the pre-verification 2/AVOID score for
  the contents API.
- Raw:
  ```json
  {"verdict":"PASS","winning_placement":"header-only","attempts":[{"placement":"header-only","list_status":200,"put_status":201,"get_status":200,"roundtrip_ok":true}]}
  ```

## ①b Chunked upload + ranged download round-trip → Pillar 3a ✅ PASS (2026-06-10)

- `ContentsTransfer` round-tripped ~3.16 MB (`notebook_id=REDACTED-nb…`,
  `endpoint=gpu-t4-s-REDACTED`) with a 1 MiB chunk size — forcing the
  chunked-PUT upload path — and the download came back **byte-perfect** (SHA-256 match).
  Confirms the production transfer path live, not just single-PUT/GET.
- Raw:
  ```json
  {"transfer":{"verdict":"PASS","bytes":3158073,"roundtrip_ok":true,"decides":"Pillar 3a: chunked upload + ranged download verified live"}}
  ```
- Note: a byte-perfect round-trip is correct whether the download streamed via ranged
  `/files/` or fell back to the single contents GET; the probe does not distinguish them.
  GB-scale streaming-vs-buffering can be confirmed later by adding a `path_used` field.

## ⑩ Runtime-direct Drive checkpoint → Pillar 3b ✅ PASS (2026-06-11)

- **Live-validated:** 5 MiB uploaded resumably runtime→Drive (`id=REDACTED-drive-id…`), ranged-
  downloaded back, **SHA-256 matched on the VM** (`VERDICT PASS 5a818d905f73 …`). The
  whole Pillar 3b path works end-to-end against real Google Drive.
- Confirmed the **quota-project auto-wire**: the spike ran with `COLABCTL_QUOTA_PROJECT`
  unset (`quota_project=None`) yet succeeded — `DriveCheckpointer` read the project from
  ADC (`set-quota-project`), so no env var was needed. The earlier 403 was purely the
  setup gate.

### History (the 403 and its fix)

- First live run hit **HTTP 403 Forbidden** on the very first Drive API call
  (`files.list`) — the ADC bearer authenticated (else 401), but the Drive request is
  rejected at the **project/quota gate**: per-user (ADC) credentials must name a *quota
  project* with the Drive API enabled, or Google tries to bill the credential's origin
  project (gcloud's `764086051850`, where the API is disabled) → 403. This is an
  environment/setup gate, not a flaw in the transfer logic (offline-validated against a
  mock Drive server, incl. resumable chunks + ranged download).
- **Two real fixes shipped in response:** the helper now (a) returns the actual HTTP
  status+body instead of a bare traceback (so the cause is visible), and (b) sends
  `x-goog-user-project` when a `quota_project` is configured. Also fixed a genuine bug —
  the ranged download GET was missing its `Authorization` header.
- **To re-validate (user):** enable Drive API on a project you own and pass it:
  ```bash
  gcloud services enable drive.googleapis.com --project=YOUR_PROJECT
  COLABCTL_ENABLE_NATIVE=1 COLABCTL_QUOTA_PROJECT=YOUR_PROJECT \
    uv run --extra native python spikes/phase_a_drive.py
  ```
  A plain re-run (no quota project) now prints the exact 403 body for confirmation.
- **Open implication:** the same ADC→Drive quota gate applies to the client-side
  `DriveSync` (never live-validated); both need the quota project. Recorded for when the
  Drive path is exercised in anger.

## canary — live HEALTHY + `ccu-info` shape captured (2026-06-11)

- `spikes/canary.py` ran end-to-end: allocate T4 → fingerprint raw shapes → exec
  (`6*7=42`) → contents-API file round-trip → teardown, CLI version matched the pin
  (0.5.7). Baseline established (`spikes/canary-baseline.json`) — `CANARY HEALTHY`.
- **Bonus — the undocumented `ccu-info` shape is now known**, which unblocks the spend
  guard:
  ```json
  {"assignmentsCount":"int","consumptionRateHourly":"float","currentBalance":"float",
   "eligibleGpus":["str"],"eligibleTpus":["str"]}
  ```
  i.e. compute-unit balance, hourly burn rate, and the entitled GPU/TPU list — enough to
  build a real pre-allocation spend guard (§5.1).

## ⑤/⑧ Browser sidecar (Track A) → PROTOCOL IDENTIFIED (2026-06-11)

The DEBUG-logged handshake nailed it. Colab's **"Connect to a local Colab MCP server"**
connects to the local WebSocket **as an MCP client**:

```
< GET /?access_token=<mcpProxyToken> HTTP/1.1
< Origin: https://colab.research.google.com
< Sec-WebSocket-Protocol: mcp          ← REQUIRES the `mcp` subprotocol negotiated
> 101 Switching Protocols              ← we accepted but did NOT echo `mcp` …
< EOF                                   ← … so the client dropped instantly
```

- **Recipe:** WebSocket, subprotocol **`mcp`**, token via **`?access_token=` query param**
  (not a hello message), Origin `colab.research.google.com`. A server that doesn't
  *negotiate* `mcp` is disconnected immediately ("disconnected from local mcp server").
- **Direction matters:** Colab is the **MCP client**; our process is the **MCP server**.
  This is "let Colab use your local tools" — *not* a browser proxy that executes our
  requests with the page's cookies. So it may **not** serve the Track-A keep-alive goal
  (which needs the page to call `KeepAliveAssignment` on our behalf). The decisive evidence
  is what the client declares in `initialize` / asks for — capture it next.
- **Role resolved (2026-06-11):** once `mcp` was negotiated, the connection stayed open and
  Colab's first (and only) frame was `notifications/tools/list_changed` — an MCP
  *server→client* notification. So **Colab is the MCP server exposing its own tools** and
  *we* are the client; it was waiting for us to drive `initialize` → `tools/list`. This is
  the promising direction: if Colab exposes a runtime-control / keep-alive tool through the
  authenticated page, Track A's keep-alive sidecar is viable.
- **DECIDED (2026-06-11) — Track A is viable, and it's bigger than keep-alive.** Captured
  `serverInfo: {name:"ColabMCP", version:"1.0.0"}` and its tools:
  `add_code_cell`, `add_text_cell`, `update_cell`, `delete_cell`, `move_cell`,
  `get_cells` (with `includeOutputs`), and **`run_code_cell`** ("Executes the code in the
  cell … output is returned"). This is a **sanctioned, first-party way to execute code in a
  Colab runtime through the logged-in browser tab** — exactly the browser-bridge transport
  (P14), now on the *real* protocol. And it answers the keep-alive question: running a no-op
  cell is genuine kernel activity in the **authenticated session**, the one principal that
  can defer idle reclamation (token auth can't) — so the browser transport can honestly
  report `keepalive=True`.
- **Build:** `BrowserBridgeTransport` rebuilt on `McpClient` + the ColabMCP tools
  (`transport/browser/`): execute = add/update + `run_code_cell`; keep-alive = no-op cell;
  upload/download ride cell exec; `keepalive=True`, `notebook_execution=True`, sanctioned
  (not opt-in). Corrects P14's guessed JSON-RPC. Caveat: not headless (needs the tab open)
  and no runtime-terminate tool (close the tab to release the VM).

## ② Same-`nbh` token refresh → §5.10 ✅ PASS

- **same_runtime=true, fresh_token=true** — re-running the assign GET pre-flight with the
  stored `notebook_id` returned the SAME endpoint with a brand-new proxy token
  (`…jeKw` → `…Uotw`, both len 229).
- **Decision:** **Non-disruptive token refresh works.** §5.10: rename
  `reassign_before_expiry` → `refresh_before_expiry`; this is also the primitive native
  *attach* uses to reconnect cold.
- Raw:
  ```json
  {"verdict":"PASS","same_runtime":true,"fresh_token":true,"endpoint_1":"gpu-t4-s-REDACTED","endpoint_2":"gpu-t4-s-REDACTED"}
  ```

## ③ Websocket reconnect to surviving kernel → §5.6 ✅ PASS

- Dropped the kernel websocket, re-dialed the SAME `kernel_id`
  (`REDACTED-kernel-uuid`); `print(x)` after reconnect returned `42` —
  **state survived the disconnect.** Validates §5.6 and the whole "connection is not the
  data plane" thesis underpinning Pillar 2.
- Raw:
  ```json
  {"verdict":"PASS","kernel_id":"REDACTED-kernel-uuid","post_reconnect_stdout":"42\n"}
  ```

## ④ /api/kernels + interrupt route → §5.3 ✅ PASS

- `GET /api/kernels` → 200 (listed the live kernel); `POST /api/kernels/{id}/interrupt`
  → **204** (route works on an idle kernel). Interrupt (§5.3) and reconnect-by-id (§5.6)
  are REST-feasible through the proxy.
- Raw:
  ```json
  {"verdict":"PASS","list_status":200,"kernels_seen":["REDACTED-kernel-uuid"],"interrupt_status":204}
  ```

## ⑤ A100 entitlement → carried Phase-0 TODO ✅ PASS

- **Entitled: YES** — `client.assign(A100)` succeeded (`gpu-a100-s-REDACTED`),
  released immediately. The allocation ladder (§5.2) can target A100 on this account.
- Raw:
  ```json
  {"verdict":"PASS","entitled":true,"endpoint":"gpu-a100-s-REDACTED"}
  ```

## ⑥ SAPISIDHASH cookie keep-alive → Track B

- **Verdict:** _PASS / FAIL / SKIPPED_ · HTTP status=_?_
- **Working header/cookie matrix (if PASS):** _…_
- **If FAIL — body / next hypothesis:** _…_
- Raw:
  ```json
  ```

## ⑦ Idle-window measurement → keep-alive necessity + auto-resume sizing

| mode | interval | reclaimed at (min) | survived full window? |
|------|----------|--------------------|------------------------|
| activity | | | |
| silent | | | |

- **Takeaway:** _does kernel activity meaningfully defer reclamation? by how much?_

## ⑧ Browser sidecar protocol capture → Track A (+ corrects P14)

- **Frames the frontend actually sends (hello + methods/results):**
  ```
  ```
- **Corrected JSON-RPC method/result shapes for BrowserBridgeTransport:** _…_

---

## Decisions locked by Phase A

- Pillar 3a transport path: _…_
- §5.10 token strategy: _…_
- Keep-alive Track A protocol / Track B recipe: _…_
