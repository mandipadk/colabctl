# Phase 0 — Findings & Go/No-Go

**Date:** 2026-05-31 · **Account:** a Colab Pro account (redacted) · **Verdict: 🟢 GO**

The sanctioned path (`google-colab-cli` v0.5.7 over ADC) **allocates a GPU, runs code, transfers files, and tears down cleanly** end-to-end. One material limitation found — **keep-alive is broken under ADC** — which *validates the directive* to build our own native transport rather than depend on the CLI. Everything below is grounded in the live transcript (`phase0-results.txt`) and the installed CLI source.

---

## 1. Confirmed working (live)

| Area | Result |
|------|--------|
| Install | `uv tool install --python 3.13 google-colab-cli==0.5.7` clean. **`jupyter-kernel-client==0.9.0` resolved from PyPI** (upstream, not the git fork) — dependency ambiguity resolved. |
| Auth | **ADC is the working path.** `gcloud` *forces* `cloud-platform` into the scope set; final granted scopes include `colaboratory` + `drive.file`. Audience = gcloud's ADC client `764086051850-…`. Self-registered OAuth2 not needed. |
| GPU runtime | `colab new --gpu T4` → `Session READY`. VM: **Tesla T4, 15.6 GB VRAM**, driver 580.82.07, CUDA 13.0 / torch `2.11.0+cu128`, `torch.cuda.is_available()=True`, **real on-GPU matmul executed**. VM Python **3.12.13**, 2 vCPU, 12.7 GB RAM, 253 GB disk. |
| File I/O | `upload`/`download` round-trip **byte-identical** (empty diff). Saved matplotlib PNG retrieved intact. |
| Lifecycle | `new` → `status` → `exec` → `upload/ls/download` → `sessions` → `log` → `stop` all succeeded; teardown left **no leaked sessions**. |
| A100 | **Untested** (skipped to conserve compute units). `new()` maps unknown `--gpu` → A100; backend returns **HTTP 400** when unentitled, surfaced as a friendly message. |

---

## 2. The keep-alive defect (most important finding)

**Symptom (from `colab log`):**
```
KEEP: error iter=1 status=403 ... KeepAliveAssignment
  "Caller does not have required permission to use project 1014160490159 ...
   roles/serviceusage.serviceUsageConsumer ... serviceusage.services.use ..."
```

**Diagnosis (from source):**
- `client.py :: keep_alive_assignment()` POSTs to `colab.pa.googleapis.com/$rpc/…/KeepAliveAssignment` with a **public web-client API key** (`x-goog-api-key: AIzaSyA2BvntLwNwFthUB4w6_Bhn0cMlVHwyaHc`) **and** `x-goog-user-project: 1014160490159` (Colab's project) — *on top of* the ADC **OAuth Bearer** that the `AuthorizedSession` always attaches.
- The Bearer carries `cloud-platform`, so Google performs an **IAM `serviceusage.services.use` check against Colab's project `1014160490159`**, which a normal user fails → **403**. (Without the user-project header you instead get a 400 "API Key and credential are from different projects" — a catch-22 for ADC user creds.)
- This is **NOT** a missing-scope problem — `whoami` confirms `colaboratory` is present. The CLI's scope pre-flight (`session.py`) correctly does *not* treat it as fatal, so the session comes up `READY`; the detached daemon then hits the same 403 twice and exits (`consecutive_4xx ≥ 2`).

**Impact:** short, attended workflows (allocate → run → stop) are **fine**. **Long-running / unattended** sessions under ADC are **not kept alive** — the VM is reclaimed at Colab's normal idle/lifetime timeout.

### ✅ Spike A.1 — RESOLVED LIVE (2026-06-01): the keep-alive RPC is dead under token auth

Tested both paths against a real T4 via the native transport (`spikes/native_smoke.py`):

| Keep-alive attempt | Result |
|--------------------|--------|
| **Bearer** (CLI's approach: OAuth token + `x-goog-user-project`) | **HTTP 403** — "Caller does not have required permission to use project 1014160490159" (serviceusage). Reproduced exactly. |
| **API-key-only** (our hypothesized fix: `x-goog-api-key`, no `Authorization`) | **HTTP 401** — "API keys are not supported by this API. Expected OAuth2 access token … that assert a principal." |

**Conclusion:** the hypothesized API-key fix is **wrong**. The browser web client succeeds only because it authenticates with the user's **Google session cookies** (SAPISIDHASH), which simultaneously assert a principal *and* carry implicit access to Colab's project — neither reproducible from an ADC token. **The RuntimeService keep-alive RPC is unusable from any token-based auth (CLI or native).**

**What we actually do (implemented):**
1. **Kernel-activity keep-alive** — `NativeColabTransport.keep_alive()` executes a trivial statement to register kernel activity. Best-effort (idle-reclamation responds to activity); full lease-extension behavior over a 90-min idle window is unverified.
2. **Checkpoint/resume + re-assign** — the reliable path for long jobs: externalize state to Drive/GCS and re-assign on reclamation (the runtime-lifecycle manager owns this). Never fake "active programming" — abuse-detection + ToS.
3. A cookie-auth keep-alive path is possible in principle but is **not** built (cookie/SAPISIDHASH scored 1.5/AVOID, and DBSC kills it in-window).

Capabilities now honestly report `keepalive=False` on the native transport, with these caveats.

**Also live-validated in the same run:** native `/tun/m/*` allocation (incl. the integer-`variant` wire mapping), native Jupyter-kernel execution (`PY 3.12.13 / CUDA True Tesla T4 / SUM 4950`), and clean unassign — the whole from-scratch transport works end-to-end.

---

## 3. Verified native transport recipe (the crown jewel)

Confirmed by reading the *installed* `colab_cli/{client,runtime,auth}.py`. This is exactly what `colabctl`'s native `/tun/m/*` adapter implements (it is **Apache-2.0**, so we may port it).

- **Hosts:** frontend `https://colab.research.google.com`; API `https://colab.pa.googleapis.com`.
- **Constants:** `TUN_ENDPOINT = "/tun/m"`; XSSI prefix `)]}'\n` (strip before JSON parse); standard headers `Accept: application/json`, `X-Colab-Client-Agent: colab-cli`, XSRF header **`X-Goog-Colab-Token`**; `authuser=0` appended for the colab.research.google.com host.
- **Notebook hash (`nbh`):** `str(uuid4())` → replace `-`→`_`, right-pad to 44 chars with `.`.
- **Assign:** `GET /tun/m/assign?nbh=<nbh>[&variant=GPU][&accelerator=T4]` returns either an existing `Assignment` or a `GetAssignmentResponse{token}`; then `POST` the same URL with header `X-Goog-Colab-Token: <token>` → `PostAssignmentResponse{endpoint, runtimeProxyInfo{token, tokenExpiresInSeconds, url}, accelerator, variant}`. **HTTP 412 → TooManyAssignmentsError.** **HTTP 400 + accelerator requested → not entitled (e.g. no A100).**
- **Accelerator enum:** `NONE, G4, T4, L4, A100, H100, V5E1, V6E1`. **Variant:** `DEFAULT, GPU, TPU`. **Shape:** `STANDARD=0, HIGH_RAM=1`.
- **Kernel connection:** `jupyter_kernel_client.KernelClient(server_url=runtimeProxyInfo.url, token=<proxy_token>, client_kwargs={subprotocol: DEFAULT, extra_params: {"colab-runtime-proxy-token": <proxy_token>}}, headers={"X-Colab-Client-Agent":"colab-cli", "X-Colab-Runtime-Proxy-Token": <proxy_token>})`. Set `_own_kernel = False` so closing the client doesn't kill the kernel. Execute via `execute` / `execute_interactive(output_hook=…)`; outputs are standard Jupyter (`stream`, `execute_result`, `display_data`, `error`).
- **Auth:** `google.auth.default(scopes=PUBLIC_SCOPES)` → `AuthorizedSession` (Bearer auto-attached). `PUBLIC_SCOPES = [openid, userinfo.profile, userinfo.email, colaboratory, drive.file]`. Token cache (oauth2 mode) at `~/.config/colab-cli/token.json`; loopback port 8200.

---

## 4. CLI stdout grammar (for the adapter parser)

The CLI has **no `--json`** — our `cli` transport adapter parses these exact shapes (single source of truth: `session.py::_format_session_line`):

```
[colab] Creating session 'NAME'...
[colab] Session READY.
[NAME] ENDPOINT | Hardware: T4 | Variant: GPU | Status: IDLE        # `status`  (Status only here)
[NAME] ENDPOINT | Hardware: T4 | Variant: GPU                        # `sessions` (no Status)
  Last Execution: FILE[ | Cell: N] at TIME                           # `status`, optional 2nd line
[colab] No active sessions found on server.                          # `sessions`, empty
[colab] Uploaded 'LOCAL' to 'REMOTE'
[colab] Downloaded 'REMOTE' to 'LOCAL'
[colab] Stopping session 'NAME'...
[colab] Session terminated.
[colab] Backend rejected accelerator 'A100'. ...                     # stderr, exit 1
```
- `Hardware`: `CPU` when accelerator is `NONE`, else the accelerator name. `Status`: `IDLE` or `BUSY (<file>)`.
- `ls` prints bare path lines (`dir/`, `file.ext`).
- **Correction to the runbook:** `colab run -` does **not** read stdin (`Script not found: -`); `run` takes a file path, `exec` takes stdin or `-f FILE`.

---

## 5. Phase 1 implications & what we build first

1. **Auth layer leads with ADC** (`google.auth.default` + `colaboratory` scope), exactly as validated. Document the mandatory `cloud-platform` + `--scopes` incantation.
2. **CLI adapter** is the sanctioned-default transport — built behind the `TransportAdapter` interface with a tolerant, golden-file-tested stdout parser pinned to v0.5.7.
3. **Native `/tun/m/*` adapter** is co-primary (opt-in), and is where we **fix keep-alive** — the concrete payoff of the no-CLI-lock-in directive.
4. **Runtime-lifecycle** design must assume keep-alive may be unavailable: checkpoint/resume + re-assign, surfaced honestly.
5. **A100** entitlement is a TODO live-check (cheap, one `colab new --gpu A100`); design already surfaces quota outcomes.

Build order this increment: `errors` + `models` (mirror the verified protocol) → `transport.base` contract → `transport.cli.parser` (+ adapter) tested against the real transcript → `transport.native.client` (verified recipe, offline-tested pure helpers).
