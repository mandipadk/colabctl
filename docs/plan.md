# The 1x → 10x Plan — From Attended Client to Durable Colab Fabric

> **Status:** accepted plan, not yet implemented. Recorded 2026-06-09 after a full review of the
> Colab path (native transport, CLI adapter, browser bridge, lifecycle, Drive sync, SDK, CLI, MCP).
> This document governs the next implementation increment the way `DIRECTIVES.md` governed v0.1/v0.2.
> Where this plan and older docs disagree, this plan wins (it *refines* `DIRECTIVES.md`; it does not
> overturn the sanctioned-default ToS posture or the no-CLI-lock-in directive).

---

## 0. Owner decisions recorded in this plan (2026-06-09)

| # | Decision |
|---|----------|
| **D1** | **Do not broaden the backend matrix.** No new backends (Kaggle, RunPod, vast, hyperscalers), no live-validation investment in the deferred ones (HF Jobs live check stays parked), no notebook-parity features (papermill/nbclient adapters), and the CLI-transport parser is **maintenance-only** (track upstream pins; no new features). Modal and Vertex are maintained as-is but not expanded. All engineering effort goes into the Colab core described below. |
| **D2** | **Keep-alive is a two-track workstream.** The browser keep-alive **sidecar** (Track A, defensible: the user's own logged-in tab, Google's own colab-mcp model) and **cookie/SAPISIDHASH** auth (Track B) are developed **together, both to proper production quality**. This **supersedes** the `1.5 / AVOID` scores for "Reverse-engineered Google cookie + SAPISIDHASH authentication" and "Local browser cookie extraction" in `DECISIONS.md` — elevated by the owner to *opt-in, disclosed-risk, co-developed*. Track B ships behind its own opt-in gate (separate from the native-transport gate), with the DBSC/ToS/ban risks documented first-class, never hidden. |
| **D3** | **The 10x thesis:** durability must come from *the runtime and local persistent state*, not from the connection. Sessions survive process exit; jobs survive disconnects and reclamation; checkpoints move real ML state. Keep-alive then becomes an optimization, not an existential dependency — but per D2 we build it properly anyway. |

These extend (do not replace) the locked decisions in `DIRECTIVES.md`: sanctioned default,
Colab Pro as the literal target, no CLI lock-in, "be prepared to write everything from scratch."

---

## 1. Where we are (the honest 1x)

What exists is a well-engineered, **attended, single-process, ephemeral remote-exec client**:

- Verified `/tun/m/*` protocol client with live-validated allocate → kernel exec → teardown
  (`src/colabctl/transport/native/client.py`, `spikes/PHASE0-FINDINGS.md` §3).
- A clean `TransportAdapter` seam (`src/colabctl/transport/base.py`) honored by three transports
  (cli / native / browser) and consumed by SDK, CLI, MCP, and the backend layer.
- An honest keep-alive diagnosis (the RuntimeService RPC is dead under token auth — 403 bearer /
  401 api-key, live-confirmed) and a lifecycle manager that re-assigns + restores on reclamation.

The gap: **every piece of load-bearing state lives in the wrong place** — the client process's
memory and a single serialized kernel channel. Every problem in §2 is a manifestation of that one
architectural fact, and every pillar in §3 moves state to one of the two places that survive: a
**local persistent store** and **the runtime itself**.

---

## 2. Current problems (complete inventory)

Each problem lists *where*, *what*, and *which fix* (pillar/item in §3–§5) resolves it. File/line
references are as of v0.2.0 (commit `8c991a7`).

### P1 — Native sessions die with the process; `stop` silently leaks runtimes ⚠ worst bug

- `NativeColabTransport` tracks sessions only in `self._sessions: dict`
  (`transport/native/adapter.py:89`). Nothing — name, notebook UUID, endpoint, proxy URL/token,
  expiry — is persisted anywhere.
- The advertised workflow `colabctl --transport native new` → later `exec -s NAME`
  (`cli.py:142` even says *"attach later with `exec -s`"*) is **broken**: the second process
  raises `RuntimeUnavailableError("No such native session")` from `_require`
  (`adapter.py:243-247`).
- **Worse:** `stop()` (`adapter.py:210-216`) does `self._sessions.pop(name, None)` and silently
  returns when the name is unknown. So a second process running
  `colabctl --transport native stop X` prints **"Stopped X."** while doing *nothing* — the
  assignment keeps burning compute units server-side. Silent money leak.
- `status()` (`adapter.py:180-182`) returns only the cached in-memory record → `None`
  cross-process; `list_sessions()` (`adapter.py:166-178`) maps unknown endpoints to
  `status=UNKNOWN` with no liveness probing and no name recovery.
- The protocol *supports* reattach — `GET /tun/m/assignments` lists live assignments, and the
  assign GET pre-flight returns an existing `Assignment` **with `runtimeProxyInfo`** when called
  with the *same* `nbh` (`client.py:294-299`) — but `allocate()` generates a fresh
  `uuid.uuid4()` every call (`client.py:288`) and discards it, so no reattach path exists.
- → Fixed by **Pillar 1** (§3.1) + the `gc`/reconcile item (§5.8).

### P2 — Everything serializes through one kernel execute channel; the lifecycle manager's promises quietly break

- Jupyter execute requests queue on the shell channel. While a 3-hour training cell runs:
  - the keep-alive tick's `kernel.execute("None")` (`adapter.py:218-230`) **blocks behind it**,
    in a worker thread (`asyncio.to_thread`, `kernel.py:168`) **with no timeout** — the
    keep-alive loop (`lifecycle.py:187-200`) stalls for the duration;
  - the checkpoint hook (`lifecycle.py:175-176`) also executes code on the same kernel, so **no
    checkpoint can run during exactly the window when checkpointing matters most**. If the
    runtime is reclaimed mid-cell, the "latest checkpoint" predates the work.
- Checkpoint/re-assign is the project's *official answer* to the keep-alive defect
  (`PHASE0-FINDINGS.md` §2, "What we actually do"), and it cannot fire while work is in flight.
- → Fixed by **Pillar 2** (§3.2: the kernel becomes a control plane); hardened by §5.5
  (tick timeouts, skip-ping-when-busy).

### P3 — File transfer cannot carry real ML state

- Upload embeds the **entire file as a base64 literal inside the code string**
  (`kernel.py:92-100` — `json.dumps(b64data)` into the source), sent as one websocket
  `execute_request`. Practical ceiling: single-digit MB (tornado's default ~10 MiB websocket
  message cap, minus base64's +33% and JSON framing), whole payload resident in memory on both
  ends.
- Download `print()`s base64 to stdout in one stream output (`kernel.py:103-111`) — same
  ceilings, plus the entire blob lands in the in-memory `ExecutionResult`.
- The native capabilities caveat admits it: *"large files are not yet streamed/chunked"*
  (`adapter.py:132-133`).
- → Fixed by **Pillar 3a** (§3.3).

### P4 — Drive checkpoints double-hop through the laptop

- `drive_checkpoint_hooks` (`drive.py:196-216`) moves every checkpoint
  **runtime → local tempfile → Drive** (and back for restore), and the runtime→local leg rides
  the P3 kernel-base64 path. Checkpointing 2 GB of weights is effectively impossible; the
  realistic envelope today is kilobytes-to-low-megabytes.
- `DriveSync` itself is non-chunked (`get_media().execute()`, acknowledged at `drive.py:9-10`)
  and buffers whole files in memory (`MediaInMemoryUpload`, `drive.py:100-105`).
- **Coherence failure:** the keep-alive answer (checkpoint/re-assign) depends on a transfer
  mechanism that can't carry checkpoints. → Fixed by **Pillar 3b** (§3.3).

### P5 — Reclamation detection is too coarse; transient blips destroy warm GPUs

- `_RECLAIM_ERRORS = (RuntimeUnavailableError, AllocationError, TransportError)`
  (`lifecycle.py:45`) — a bare `TransportError` (one network blip, one 5xx that exhausted
  retries, one dropped websocket) triggers a **full re-assign**: the warm, healthy A100 is
  released and the code re-run from scratch (`lifecycle.py:143-166`).
- No probe distinguishes "runtime actually gone" (endpoint absent from `/tun/m/assignments`,
  kernel dead) from "connection hiccup."
- A dropped websocket **mid-cell** additionally loses the execution result even when the VM and
  the computation are fine — `NativeKernel` has no reconnect path (`kernel.py:141-241`); the
  Jupyter kernel survives server-side (`_own_kernel = False`, kernel_id retained at
  `kernel.py:196-199`) but we never re-dial it.
- → Fixed by §5.4 (reclaim probe + error narrowing) and §5.6 (websocket reconnect); largely
  defused by **Pillar 2** (a reconnect costs a log-tail resume, not a job).

### P6 — No cancel/interrupt

- `KernelProtocol` has `restart` but nothing exposes **interrupt**; no transport, SDK, CLI, or
  MCP surface can cancel a runaway cell short of killing the whole runtime. The runtime proxy is
  a Jupyter server — `POST /api/kernels/<id>/interrupt` exists, and `NativeKernel` already holds
  `kernel_id` (`kernel.py:150,198-199`). For the agent-facing product (MCP is a first-class
  consumer), cancel is table stakes. → §5.3, plus job-level `cancel` in **Pillar 2**.

### P7 — No live logs, weak status, no quota awareness

- `ColabBackend.logs()` returns nothing until completion — `logbuf` is appended **once**, after
  the result (`backends/colab.py:148`); `streaming_logs=False` (`colab.py:70`).
- `ColabBackend.capabilities` claims **`persistent=True`** (`colab.py:71`) while its own
  docstring admits job tracking is in-process and lost on death (`colab.py:8-10`). Dishonest
  capability — the rest of the codebase is scrupulous about honesty.
- `ccu_info()` exists (`client.py:324-330`) but is surfaced **nowhere** — no `colabctl quota`,
  no spend awareness before allocating an A100, even though compute units are *the* resource a
  Colab Pro user manages. → **Pillar 2** (real logs), §5.1 (quota), §5.7 (honesty fixes).

### P8 — No allocation policy

- `AcceleratorUnavailableError` is raised on the 400 (`client.py:305-311`) and that's the end.
  Colab Pro users hit GPU stockouts constantly; there is no fallback ladder (A100 → L4 → T4),
  no retry-with-backoff, no wait-for-availability mode. `TooManyAssignmentsError` (412) likewise
  has no recovery UX (list/reclaim orphans) — particularly bad combined with P1's leak.
  → §5.2 and §5.8.

### P9 — In-memory output accumulation

- `ExecutionResult` aggregates **all** outputs in memory (`models.py:190-233`); hours of training
  logs through `execute()` balloon the client process. The CLI transport similarly buffers entire
  subprocess stdout (`transport/cli/adapter.py:143-152`). → **Pillar 2** (logs spool on the VM's
  disk; the client tails incrementally) + §5.9 (ring-buffer cap for interactive execs).

### P10 — Proxy-token expiry handled by disruption, not refresh

- `seconds_until_proxy_expiry` exists (`adapter.py:154-164`) but the only consumer action is a
  **disruptive full re-assign** (`lifecycle.py:177-178`, off by default). The assign GET
  pre-flight with the *same* `nbh` should mint fresh `runtimeProxyInfo` for the *same* runtime —
  a non-disruptive token refresh. Unverified live; needs the §6 Phase-A spike. → §5.10.

### P11 — Single-account assumption

- `authuser=0` is hardcoded (`client.py:231-232`); `ADCAuthProvider` binds to whatever ADC
  resolves; nothing in state, naming, or the secret store keys by account. Multi-account (work +
  personal Pro) is a real Colab usage pattern. → §5.11 (kept deliberately small; not a pillar).

### P12 — MCP server inherits every per-process limitation

- `ColabTools` (`mcp_server.py:61-104`) allocates with `keep=True` and tells the agent to reuse
  the session name — but if the MCP server restarts, native sessions become unreachable (P1) and
  `stop_runtime` silently lies. `JobTools.run_job` (`mcp_server.py:128-152`) is synchronous
  (allocate → run → return): no submit-and-poll, so a long job monopolizes one tool call and dies
  with the server. → **Pillars 1–2** make the fixes; §4 adds the new MCP tool surface.

### P13 — No protection against silent protocol drift

- The native transport's dominant external risk is Google changing `/tun/m/*` or the proxy
  handshake. The CLI side has `PINNED_CLI_VERSION` probing (`transport/cli/adapter.py:198-215`);
  the native side has only hand-run spikes. Breakage will currently be discovered by users.
  → §5.12 (scheduled live canary).

### P14 — Browser bridge is scaffolding without a validated counterparty

- `BrowserBridgeTransport` (`transport/browser/bridge.py`) implements the colab-mcp relay model
  but its JSON-RPC shapes are **not live-validated** (admitted at `bridge.py:9-11,141-144`), and
  as a *full transport* it duplicates what native does better. Its unique value is the one thing
  only a browser has: **session cookies that can call KeepAliveAssignment**. → Repurposed as the
  keep-alive sidecar (**§3.4 Track A**); full-transport ambitions dropped.

---

## 3. The pillars

### 3.1 Pillar 1 — Persistent session store + native attach

**Why first:** it fixes the outright bug (P1), it is the precondition for Pillars 2–3 and the MCP
story (P12), and agents/CLIs are inherently multi-process.

**Design:**

- **Store:** `~/.colabctl/` (override: `COLABCTL_HOME`), with `state.json` (atomic
  write-tmp-then-`os.replace`, plus an advisory lock file via `O_EXCL`-style locking for
  multi-process safety — no new heavy deps; a tiny `statestore.py` module).
- **Record schema** (pydantic, versioned with `"schema_version": 1` for forward migration):

  ```json
  {
    "schema_version": 1,
    "sessions": {
      "<name>": {
        "transport": "native",
        "notebook_id": "<uuid — the nbh seed; REQUIRED for refresh/reattach>",
        "endpoint": "...",
        "proxy_url": "...",
        "proxy_token_ref": "<secret-store key — token itself is a credential>",
        "proxy_token_expires_at": "<wall-clock ISO-8601, derived from tokenExpiresInSeconds>",
        "accelerator": "T4", "variant": "GPU",
        "account": "<email or 'adc-default'>", "authuser": 0,
        "created_at": "...", "last_seen_at": "..."
      }
    },
    "jobs": { "...": "see Pillar 2" }
  }
  ```

  Subtlety: the **proxy token goes into the existing pluggable secret store**
  (`src/colabctl/secrets/`) — that abstraction was built up front in Phase 1 precisely for this;
  metadata in `state.json`, credentials in the secret store. (Cookie blobs in §3.4 Track B use
  the same rule.)
- **`allocate()`** persists the record (including the generated `notebook_id`) before returning.
- **New `NativeColabTransport.attach(name)`** (and `ColabClient.attach` grows
  cross-process-capable for native):
  1. load record → 2. reconcile against `GET /tun/m/assignments` (endpoint still listed?) →
  3. if proxy token expired/missing, re-run the assign GET pre-flight **with the stored
  `notebook_id`** to mint fresh `runtimeProxyInfo` for the same runtime (P10 spike validates
  this) → 4. reconnect the kernel (the server-side kernel survives; reuse `kernel_id` when we
  have it, else list `GET /api/kernels` via the proxy).
- **`stop()` is rewritten to never lie:** resolve via store *and* server list; if the name is
  unknown locally but an assignment matches (by endpoint), unassign it; if nothing matches,
  raise `RuntimeUnavailableError` — **no more silent success** (P1).
- **`list_sessions()`** merges server truth with stored names and probes liveness (assignment
  present? proxy token valid? optional cheap kernel ping) instead of blanket `UNKNOWN`.
- **`status()`** consults the store + an optional live probe flag, not just process memory.
- Stale-record hygiene: records whose endpoint is absent from the server list are marked
  `terminated` and pruned by `colabctl gc` (§5.8), never auto-deleted silently.
- CLI/SDK/MCP need **no signature changes** — `attach`/`exec -s`/`stop`/`status` simply start
  working across processes, which is the point.

**Acceptance:** `new` in process 1; `exec -s`, `status`, `download`, `stop` from process 2 all
work on native; `stop` of a nonexistent session errors; killing the client leaks nothing that
`sessions`/`gc` can't see and reclaim; all of this offline-tested with fakes + one live spike.

### 3.2 Pillar 2 — Detached jobs: the kernel becomes a control plane, not the data plane

**Why:** fixes P2, P5 (mostly), P6 (job-level), P7 (live logs), P9, P12 — one change, six
problems. This is the single highest-leverage feature for the agent use case: true
*submit → walk away → collect*.

**Design — runtime side.** A job lives on the VM under `/content/.colabctl/jobs/<job_id>/`:

```
script.py        # the user code (written via Pillar 3a transfer, or chunked exec fallback)
meta.json        # spec echo: accelerator, requirements, created_at, resumable flag
pid              # of the detached process-group leader
status.json      # {"state": "running|succeeded|failed", "started_at": ..., "finished_at": ...}
log.txt          # combined stdout+stderr, appended live
exit_code        # written by the wrapper on completion (file presence == finished)
```

- **Launcher** (executed via one short kernel exec): write files, then
  `setsid nohup python -u runner.py & echo pid`. `runner.py` is a small wrapper that runs
  `script.py`, tees output to `log.txt`, and writes `exit_code`/`status.json` on exit — so job
  truth lives **on the VM's disk**, not in any connection. `pip install` of requirements happens
  inside the wrapper (logged to the same spool), not as a separate fragile kernel exec
  (replaces `backends/colab.py:33-38,136-142`).
- **Polling** is a sub-second kernel exec (read `status.json`/`exit_code`) — the kernel is
  otherwise **free**, so keep-alive ticks and checkpoint hooks run *during* the job (kills P2).
- **Log tailing** by byte offset: kernel exec does `seek(offset); read(n)` and returns a
  base64-framed chunk (reusing the existing marker-framing helpers in `kernel.py`); the client
  persists `log_offset` per job, so `--follow` resumes exactly where it left off after any
  disconnect, laptop sleep, or process restart.
- **Cancel:** `os.killpg(pid)` (SIGINT → grace period → SIGKILL) via kernel exec; state goes to
  `cancelled` in `status.json`.

**Design — client side.**

- Job records persist in the Pillar-1 store (`jobs` map): job_id → session name, spec, state
  cache, `log_offset`. `ColabBackend` is rebuilt on this: `submit` returns immediately after
  launch; `status`/`logs`/`result`/`cancel` work **from any process** (fixes the in-process
  limitation admitted at `colab.py:8-10`, and makes `persistent=True` true — P7).
- **Lifecycle integration (auto-resume):** on confirmed reclamation (§5.4 probe), the manager
  re-assigns, runs the restore hook (checkpoints now real via Pillar 3), and **relaunches the
  persisted job spec** — opt-in via `resumable=True` on the spec, because only the user knows
  the script resumes from its checkpoint idempotently. The job_id survives across runtimes;
  `status.json` history records each incarnation.
- Subtlety: while a detached job is `running`, the keep-alive tick **skips** the `execute("None")`
  ping — the job itself is kernel-host activity, and the poll exec already touches the kernel
  (§5.5).

**Surface:**

- CLI: `colabctl job run --detach`, `job status <id>`, `job logs <id> [--follow]`,
  `job result <id>`, `job cancel <id>`, `job list`.
- MCP: `submit_job`, `job_status`, `job_logs` (offset-aware), `job_result`, `cancel_job` — the
  agent pattern becomes submit → do other work → poll, instead of one blocking `run_job` call
  (P12).

**Acceptance:** submit a 30-min job; kill the client; from a new process, `job logs --follow`
resumes and `job result` returns the exit code. Pull the network mid-job; reattach succeeds; the
job never noticed. Reclamation with `resumable=True` relaunches and completes. All
offline-simulated with a fake kernel/VM; one live spike for the real path.

### 3.3 Pillar 3 — Transfer that moves real ML state, runtime-direct checkpoints

**3a — Replace kernel-base64 transfer (P3).** Preferred path: the **Jupyter contents REST API on
the runtime proxy** — `GET/PUT {proxy_url}/api/contents/<path>` with the already-verified header
recipe (`X-Colab-Runtime-Proxy-Token` + `X-Colab-Client-Agent`), using the contents API's chunked
upload protocol (`chunk: 1..n, -1`) for streaming with bounded memory.

- ⚠ **Contingent on a Phase-A spike:** `DECISIONS.md` scored "Notebook/file sync via Jupyter
  Contents API through the proxy" 2/AVOID and "x-colab-tunnel anti-XSS reverse-proxy access"
  2.5/AVOID — those assessments predate the Phase-0 verification of the proxy header recipe, but
  the proxy may still wrap/block non-kernel REST routes. The spike probes `/api/contents` (and
  `/api/kernels`, needed for §5.3/§5.6) through the live proxy.
- **Fallback if blocked** (design committed now so the spike can't strand us): **chunked
  kernel-exec transfer** — split base64 into ~1 MiB chunks across multiple `execute` calls,
  append server-side to a temp file, verify length + SHA-256, atomic rename. Slower than REST but
  removes the message-size ceiling and the whole-file-in-one-literal pathology with zero new
  protocol surface. Either way, `upload`/`download` grow progress callbacks and integrity checks.

**3b — Runtime-direct Drive checkpoints (P4).** Cut the laptop out of the loop:

- Client mints a **short-lived access token downscoped to `drive.file`** from the existing ADC
  credentials (the scope is already in `COLAB_SCOPES`), injects it to the runtime via kernel exec
  into a `0600` file (never a code literal that lands in logs, never an env var visible in
  `/proc`).
- A small runtime-side helper (stdlib + `requests`, both present on Colab) does **resumable
  Drive uploads** (`uploadType=resumable`, 8–16 MiB chunks) directly runtime → Drive, and ranged
  `alt=media` downloads for restore. `drive_checkpoint_hooks` is reimplemented on this; the old
  double-hop path remains only as a documented fallback for non-native transports.
- Token lifetime ~1 h: the checkpoint tick re-injects a fresh token each cycle; on 401 the helper
  writes a `token-expired` marker the next tick detects. Security posture documented: blast
  radius is `drive.file`-only (files the app created), token short-lived, file perms 0600.
- `DriveSync` (client-side) also gains chunked/resumable transfer for its own paths
  (`drive.py:9-10` acknowledges this gap).

**Acceptance:** upload/download a 500 MB file through 3a (or fallback) with bounded memory and a
verified hash; checkpoint 2 GB of weights runtime→Drive in the background **while a job runs**;
reclamation + restore round-trips it. This is the moment "checkpoint/re-assign" becomes an honest
answer to the keep-alive defect.

### 3.4 Keep-alive — two tracks, co-developed (owner decision D2)

Context (`PHASE0-FINDINGS.md` §2): the RuntimeService `KeepAliveAssignment` RPC is dead under all
token auth — 403 with bearer (serviceusage IAM check against Colab's project `1014160490159`),
401 api-key-only. Only browser **session cookies** succeed. Both tracks below produce a working
keep-alive; they share an interface so the lifecycle manager doesn't care which is active.

**Shared interface:** a `KeepAliveProvider` protocol (`async def extend(endpoint) -> None`,
`async def healthy() -> bool`) that `RuntimeLifecycleManager` consumes when configured, *in
addition to* (not replacing) kernel-activity pings and checkpoints. Capabilities flip
`keepalive=True` only when a provider is live and its last extend succeeded — honesty preserved.

**Track A — browser keep-alive sidecar (defensible play).** Repurpose
`BrowserBridgeTransport` (P14) into a minimal sidecar:

- `colabctl keepalive-sidecar` starts the local WS relay (origin-checked + token handshake — the
  hardening at `bridge.py:158-170` already exists), opens/asks for one logged-in Colab tab, and
  the page-context loop calls `KeepAliveAssignment` for the endpoints we hand it, every N
  minutes, using the browser's own cookies. Native transport keeps doing all headless work.
- It is the user's own session in the user's own browser — the same trust model as Google's
  colab-mcp. Requires the §6 Phase-A live validation of the frontend protocol (currently the
  bridge's JSON-RPC shapes are unconfirmed). Failure degrades gracefully: sidecar tab closed →
  provider reports unhealthy → lifecycle falls back to kernel-activity + checkpoint/re-assign.

**Track B — cookie/SAPISIDHASH auth (opt-in, disclosed-risk; proper engineering, not a hack).**

- **Gate:** `COLABCTL_ENABLE_COOKIE_AUTH=1` — its **own** opt-in, separate from
  `COLABCTL_ENABLE_NATIVE`, with the same loud `ConfigurationError` pattern as
  `require_native_opt_in()` (`adapter.py:55-63`) spelling out: gray-area ToS, abuse-detection
  exposure, account-ban risk, DBSC fragility.
- **Cookie sourcing** behind a `CookieSource` abstraction: (1) manual export file
  (Netscape/JSON formats) — always works, zero magic; (2) `browser_cookie3`-style local-browser
  extraction (optional extra); (3) future: DevTools-protocol grab. Cookie blobs are credentials →
  **secret store only**, per-account keys, never plaintext on disk.
- **SAPISIDHASH implementation:** `Authorization: SAPISIDHASH {ts}_{SHA1("{ts} {SAPISID} {origin}")}`
  with `Origin: https://colab.research.google.com`, cookie jar carrying `SAPISID`
  (/`__Secure-3PAPISID`) + the `SID/HSID/SSID/...` set, against
  `colab.pa.googleapis.com/$rpc/.../KeepAliveAssignment`. Exact header/cookie matrix is a Phase-A
  spike deliverable (live-verify; don't trust folklore).
- **DBSC reality (why `DECISIONS.md` scored this AVOID):** Chrome's Device Bound Session
  Credentials rotate/bind cookies to the device, so exported cookies can die fast or off-device.
  Engineering response: health-check loop, explicit `CookieAuthError` taxonomy (expired vs.
  rotated vs. challenged), refresh guidance surfaced to the user, automatic fallback to Track
  A/checkpoint path on failure. We treat "cookies die" as a *normal state to degrade from*, not
  an exception.
- Track B also unlocks (deliberately **out of scope** for now, recorded for later): cookie-auth
  `ccu-info`/assign paths, which may behave differently from token auth.

**Acceptance:** live-verified lease extension across a ≥ 90-minute *idle* window (the unverified
gap named in `PHASE0-FINDINGS.md`) for each track independently; clean degradation when the
sidecar tab closes / cookies rot; capabilities and caveats reflect the truth at runtime.

---

## 4. Surface changes summary (CLI / SDK / MCP)

| Surface | New | Changed |
|---|---|---|
| CLI | `job run --detach / status / logs --follow / result / cancel / list`; `quota`; `gc`; `interrupt -s`; `keepalive-sidecar`; `attach` (prints reattach result) | `stop` (never lies); `sessions` (live statuses, account column); `new --gpu A100,L4,T4` ladders |
| SDK | `ColabClient.attach` cross-process for native; `session.interrupt()`; `JobHandle` (detached); `KeepAliveProvider` | `ColabSession.run` (optional output ring-buffer cap) |
| MCP | `submit_job`, `job_status`, `job_logs`, `job_result`, `cancel_job`, `quota`, `interrupt` | `stop_runtime` (truthful), `list_runtimes` (probed status), `SERVER_INSTRUCTIONS` updated for the submit→poll pattern |

---

## 5. Smaller high-leverage items

> **Progress (2026-06-11):** ✅ §5.1 `colabctl quota` (friendly balance/burn/runway/eligible
> GPUs, raw fallback) **+ the spend guard** — the `ccu_info` shape (`currentBalance`,
> `consumptionRateHourly`, `eligibleGpus/Tpus`) was captured live by the canary, so
> `colabctl/spend.py` + a `--yes`-overridable guard on `run`/`new` now refuses a native
> allocation when the balance is non-positive and warns on short runway / ineligible GPU. ✅
> §5.2 allocation **ladder** (`--gpu A100,L4,T4`, `_resolve_ladder` + ladder-aware
> `ColabClient.allocate` falling through `AcceleratorUnavailableError`). ✅ §5.8 **gc-on-412**:
> `TooManyAssignmentsError` now prints a `gc --release-orphans` hint. ✅ §5.12 **canary**:
> `colabctl/drift.py` (pure structural-fingerprint drift detection, 8 tests) + `spikes/canary.py`
> (allocate→fingerprint raw `assignments`/`ccu-info`→exec→contents round-trip→teardown + CLI
> version probe, baseline compare, exit code) + `.github/workflows/canary.yml` (weekly, gated on
> `GOOGLE_ADC_JSON` secret, skips cleanly). 696 tests green. Remaining: the spend-guard heuristic
> (needs the `ccu_info` shape understood) + the deferred chunked client-side `DriveSync`.

1. **`colabctl quota` + spend guard.** Surface `ccu_info()` (`client.py:324`): best-effort typed
   fields where stable, raw dict passthrough otherwise (shape is undocumented — keep the existing
   honesty). Optional pre-allocation guard: estimated burn for the requested accelerator vs.
   balance, `--yes` to skip.
2. **Allocation ladder + stockout retry.** `--gpu A100,L4,T4` preference order;
   `AcceleratorUnavailableError` → next rung; optional `--wait <duration>` mode with capped
   exponential backoff + jitter for stockouts. Implemented *inside* the Colab path (it is not
   cross-backend routing; D1 unaffected).
3. **Kernel interrupt.** `POST {proxy_url}/api/kernels/{kernel_id}/interrupt` with proxy headers
   (kernel_id already captured, `kernel.py:198-199`); fallback if the REST route is proxied-off:
   none — document. Exposed as §4 surfaces.
4. **Reclaim probe + error narrowing (P5).** Before any re-assign, the lifecycle manager probes:
   endpoint in `/tun/m/assignments`? cheap kernel ping ok? Only confirmed-gone triggers
   re-assign; transient errors get bounded retry-in-place. `_RECLAIM_ERRORS` narrows: bare
   `TransportError` no longer auto-reassigns without probe confirmation.
5. **Keep-alive tick hardening (P2 adjunct).** `kernel.execute("None", timeout=30)` so the loop
   can never wedge; skip the ping while a detached job is running; tick telemetry via the
   existing `observability` logger.
6. **Websocket reconnect in `NativeKernel`.** On ws drop, re-dial the *same* `kernel_id`
   (server-side kernel survives) before surfacing an error; mid-cell, reconnect restores the
   iopub stream (best-effort — output during the gap may be lost; detached jobs make this moot
   for long work). Distinguish "ws died" from "kernel died" in the error taxonomy
   (`KernelError` vs `RuntimeUnavailableError`).
7. **Honesty fixes (P7).** `ColabBackend.persistent` derives from store-backed reality;
   `streaming_logs=True` only once detached jobs land; native `status()`/`list_sessions()` report
   probed truth; remove the now-false caveats as each pillar lands (and add new ones as needed —
   the caveat mechanism is good, keep using it).
8. **Orphan reclamation: `colabctl gc`.** List server assignments with no (or stale) local
   record; offer unassign. Auto-suggested when allocation hits 412 `TooManyAssignmentsError`.
   Directly mitigates the damage class of P1 for anyone on v0.2.x state.
9. **Output spooling cap.** Ring-buffer / max-bytes guard on interactive `ExecutionResult`
   accumulation with an honest truncation marker (P9); full fidelity remains available via
   detached-job log files.
10. **Non-disruptive proxy-token refresh (P10).** Spike: assign GET pre-flight with stored
    `notebook_id` near expiry → fresh `runtimeProxyInfo`, same runtime. If confirmed, the
    lifecycle manager's `reassign_before_expiry` becomes `refresh_before_expiry` (no disruption);
    if refuted, keep re-assign but document the ceiling.
11. **Multi-account plumbing (P11), minimal.** `account` + `authuser` fields threaded through
    state records and secret-store keys; `--account` flag selects; no profile system yet — just
    don't *bake in* single-account assumptions while we're touching every record anyway.
12. **Scheduled live canary (P13).** `spikes/canary.py`: allocate (cheapest viable) → exec →
    transfer probe → teardown on **both** transports + protocol fingerprints (response-shape
    hashes for assign/assignments/ccu-info). Opt-in GitHub Actions `schedule:` workflow gated on
    repo secrets (skip cleanly when absent). Converts "users discover Google broke us" into "the
    canary told us this morning." Costs a few compute-unit-minutes weekly; document that.
13. **Auth UX (DONE 2026-06-11).** Emerged from the 3b live finding (the ADC→Drive quota-project
    403). The `colaboratory` scope is not third-party-grantable, so ADC stays the native-path
    auth — but the friction is fixed: `colabctl auth login` wraps the gcloud ADC incantation
    (the right scopes), `colabctl auth status` introspects via tokeninfo and reports
    account + `colaboratory`/`drive.file` presence + quota project + exact fix hints, and
    `colabctl auth scopes` prints the command. `DriveCheckpointer` auto-reads the ADC quota
    project (`AuthProvider.quota_project_id`), so `set-quota-project` "just works" with no env
    var. ADC is one-time per machine (persists via refresh token), not per run. 680 tests green.

## 6. Sequencing

Phases are dependency-ordered; within a phase, work is parallelizable. Keep-alive (Phase E) can
start its spikes in Phase A and proceed in parallel from Phase B onward — per D2 it must not
trail as an afterthought.

| Phase | Contents | Gate to next |
|---|---|---|
| **A — Spikes & validation** (live, hand-run, cheap) | ① contents-API + `/api/kernels` via proxy (decides 3a path); ② proxy-token refresh via same-`nbh` pre-flight (decides §5.10); ③ ws-reconnect to live kernel_id; ④ SAPISIDHASH header/cookie matrix live-verify; ⑤ browser-sidecar frontend protocol validation; ⑥ kernel-activity vs. 90-min idle window measurement (the named unverified gap); ⑦ A100 entitlement check (carried TODO from Phase 0 §5) | findings appended to `spikes/PHASE0-FINDINGS.md` style doc (`spikes/PHASE-A-FINDINGS.md`) |
| **A — RESULTS (2026-06-09)** | ✅ runtime probes ①–⑦(runtime subset) + ③ ④ ⑦(A100) all **PASS** on one T4 (`spikes/PHASE-A-FINDINGS.md`): contents REST API works **header-only** → Pillar 3a is REST (fallback not needed); same-`nbh` refresh returns same runtime + fresh token → §5.10 confirmed (`refresh_before_expiry`); ws-reconnect keeps state → §5.6 confirmed; interrupt route 204 → §5.3 confirmed; A100 entitled. Still to run: ④ cookie/SAPISIDHASH, ⑥ idle-window, ⑤/⑧ sidecar capture (size keep-alive; do not gate Pillars 1–3). | — |
| **B — Pillar 1** | state store, secret-store token refs, native `attach`, truthful `stop`/`status`/`list`, `gc`, honesty fixes (§5.7, §5.8), reclaim probe + narrowing (§5.4), tick hardening (§5.5), multi-account fields (§5.11) | cross-process acceptance tests green |
| **B — DONE (2026-06-09)** | ✅ `state/` store (atomic + flock + crash/concurrency tests); `ColabBackendClient.refresh_assignment` (GET-only reattach/refresh); native `attach` (cached-token fast path + refresh fallback), truthful `stop` (confirms release, never silent no-op), probe-based `list_sessions`/`status`, `reconcile`+`gc`, secret-store proxy-token caching (graceful when absent), `account`/`authuser` fields, CLI `attach`+`gc`. 588 tests green. **Remaining in B:** §5.4 reclaim probe + `_RECLAIM_ERRORS` narrowing, §5.5 keepalive tick hardening, §5.7 ColabBackend honesty (lands with Pillar 2). | — |
| **C — Pillar 2** | runtime job layout + wrapper, detached `ColabBackend`, log tailing/follow, job cancel, interrupt (§5.3), ws reconnect (§5.6), CLI/SDK/MCP job surface, output cap (§5.9) | survive-the-client acceptance tests green |
| **C — DONE (2026-06-09)** | ✅ `jobs/codes.py` (pure launch/poll/tail/cancel builders + stdlib `runner.py` supervisor; runner proven by real local subprocess) + `jobs/runtime.py` (`KernelJobRuntime`) + `jobs/backend.py` (`DetachedColabBackend`: submit launches detached + persists `StoredJob`; status/logs(`--follow`, offset)/result/cancel/list work cross-process; **auto-resume** of `resumable` jobs on `RuntimeUnavailableError`); CLI `job run --detach/--resumable`, `job status/logs -f/result/cancel/list`; MCP `submit_job/job_status/job_logs/job_result/cancel_job` + updated instructions; honesty: detached backend now `persistent=True`/`streaming_logs=True`. 632 tests green (full lifecycle exercised hermetically via real subprocesses). | — |
| **C — TAIL DONE (2026-06-10)** | ✅ §5.3 kernel **interrupt** (`client.interrupt_kernel` via the proxy REST 204; transport `interrupt`; SDK `session.interrupt()`; CLI `colabctl interrupt`; MCP `interrupt_runtime`); §5.6 **ws reconnect** (`NativeKernel.reconnect()` re-dials the retained `kernel_id`; transport `reconnect`; `kernel_id` exposed) — re-issue only idempotent work; §5.9 **output cap** (`cap_stream_output` head+tail with truncation marker, wired into `NativeKernel.execute`). | — |
| **D — Pillar 3** | 3a transfer (REST or committed fallback per spike ①), 3b runtime-direct Drive checkpoints, chunked `DriveSync`, lifecycle auto-resume of resumable jobs, proxy-token refresh (§5.10 per spike ②) | 2 GB checkpoint acceptance test green |
| **D — 3a DONE (2026-06-10)** | ✅ `transport/native/contents.py` `ContentsTransfer`: chunked-PUT upload (JupyterLab chunk protocol, bounded memory, size-verify) + ranged `/files/` download with single-contents-GET fallback; native transport `upload`/`download` now use it (kernel-base64 path retired); capability caveat updated; `spikes/phase_a_runtime.py transfer` probe **live-validated PASS (2026-06-10)**. | — |
| **D — 3b + §5.10 DONE (2026-06-10)** | ✅ §5.10 **non-disruptive token refresh**: `NativeColabTransport.refresh_token` (GET-only, same runtime, fresh token) + lifecycle `refresh_before_expiry` preferring refresh over re-assign. ✅ **runtime-direct Drive checkpoints** (`drive_runtime.py` pure-stdlib resumable upload + ranged download, **validated by a real subprocess round-trip against a mock Drive server**; `DriveCheckpointer` in `drive.py` injects a short-lived token to a 0600 file then runs the transfer on the VM; lifecycle `hooks()`); `spikes/phase_a_drive.py` for live validation; security posture documented (token carries ADC grant scopes — no clean drive.file-only mint from a broad user grant; short-lived, re-injected, 0600, unlogged). 672 tests green. **Live-validation caveat (2026-06-11):** first live run hit a 403 at the ADC→Drive *quota-project* gate (per-user creds need a quota project with Drive API enabled); helper now surfaces the HTTP body + supports `quota_project` (`x-goog-user-project`), and a download-GET auth bug was fixed — re-validate with `COLABCTL_QUOTA_PROJECT` set (PHASE-A-FINDINGS ⑩). **Deferred (lower value now):** chunked client-side `DriveSync` (superseded by runtime-direct for the checkpoint path; the double-hop `drive_checkpoint_hooks` remains as the non-native fallback). | — |
| **E — Keep-alive tracks A + B** (parallel from B) | `KeepAliveProvider` interface; sidecar (Track A); cookie source abstraction + SAPISIDHASH + DBSC degradation (Track B); capabilities truth-wiring | ≥ 90-min idle lease extension live-verified per track |
| **F — Operational hardening** | canary (§5.12), quota + spend guard (§5.1), allocation ladder (§5.2), docs (this plan → architecture.md updates, deployment guide for sidecar/cookie setup), ROADMAP.md refresh | release |

---

## 7. Testing strategy (mirrors existing repo conventions)

- **Offline-first:** every new module gets unit + stress tests with fakes (fake kernel, fake
  store, fake Drive, fake cookie jar) — no credentials in `tests/`, same as today.
- **Golden tests** for the runtime-side artifacts (`runner.py` wrapper output, `status.json`
  shapes, log-chunk framing) — they are a *contract* between client versions and running jobs
  (an old job must be pollable by a newer client).
- **Contract tests** extended: `test_native_contract.py` grows attach/stop-truthfulness/job
  cases that any transport claiming the capabilities must pass.
- **Concurrency/crash tests** for the state store (two processes, kill -9 mid-write, lock
  contention).
- **Live spikes** stay hand-run in `spikes/` (Phase A list) + the scheduled canary as the only
  automated live touchpoint.

## 8. Risk register (delta to existing)

| Risk | Mitigation |
|---|---|
| Google changes `/tun/m/*` / proxy routes | canary (§5.12); protocol fingerprints; CLI transport remains the sanctioned fallback (maintenance-only ≠ removed) |
| Contents API blocked by the tunnel proxy | committed fallback design (chunked kernel-exec) — spike decides, neither outcome strands Pillar 3a |
| Cookie path (Track B) triggers abuse detection / ban | own opt-in gate, loud disclosure, off by default, automatic degradation; never enabled implicitly by any other flag |
| DBSC rotates/binds cookies | health-check + `CookieAuthError` taxonomy + fallback to Track A / checkpoint path; treat as normal degradation |
| Detached processes evade Colab activity heuristics → reclaim despite "busy" | measured in spike ⑥; keep-alive providers (Phase E) + auto-resume (Phase D) bound the damage either way |
| State store corruption / concurrent writers | atomic replace + lock + schema_version + crash tests |
| Drive token on runtime leaks | `drive.file`-only downscope, ~1 h lifetime, 0600 file, re-inject per tick, documented blast radius |
| Scope creep back into backend breadth | **D1 is locked.** Any new-backend proposal needs an explicit owner decision recorded here first |

## 9. Non-goals (explicit, per D1)

- No new backends; no live-validation work on HF Jobs/Kaggle/RunPod/vast/hyperscalers.
- No papermill/nbclient notebook-parity features (the existing `notebook.py` stays as-is).
- No new features in the CLI-transport parser (pin tracking + drift warnings only).
- No browser-bridge *full transport* development — sidecar only.
- No profile/config system beyond §5.11's minimal account fields.

## 10. Definition of done (the 10x test)

Today colabctl answers: *"run this code on a Colab GPU while I watch."*

After this plan it answers: *"here's a 12-hour training job — survive my laptop sleeping, the
websocket dying, and even the runtime being reclaimed; keep the lease alive if you can; checkpoint
my real model weights to my Drive either way; and have logs, status, and artifacts waiting for me
(or my agent) in any process, at any time."*

Concretely, the headline acceptance scenario, end-to-end on real Colab Pro: `colabctl job run
--detach --gpu A100,L4,T4 --resumable train.py` → close the laptop → reopen hours later →
`colabctl job logs --follow` resumes mid-stream → reclamation occurred once, the job auto-resumed
from its Drive checkpoint → `colabctl job result` exits 0 → `colabctl quota` shows the spend →
`colabctl gc` finds nothing leaked.
