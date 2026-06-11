# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to adhere to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.3.0] - 2026-06-11

The **durability** release. colabctl gains a persistent fabric so sessions and jobs
survive process exit, disconnects, and runtime reclamation — plus real-size data movement,
runtime-direct Drive checkpoints, first-class auth UX, and a sanctioned browser transport
that keeps its runtime alive. (Test suite 555 → 708.)

### Added

- **Persistent state store** (`~/.colabctl/state.json`, atomic write + `flock`): sessions
  and jobs are durable across processes. A runtime created in one process is **attachable**
  from another (`colabctl attach`), `stop` is truthful, and `colabctl gc` reclaims orphans.
  Corruption is quarantined and the on-disk schema is versioned.
- **Detached jobs** — submit long work that outlives your client. Jobs run as a supervised
  stdlib process on the VM (the kernel is a *control plane*, not the data plane), so a
  dropped websocket costs a reconnect, not the job. `colabctl job run --detach`, with
  `status` / `logs -f` (resumes exactly after a disconnect) / `result` / `cancel` / `list`,
  mirrored in the SDK and MCP server (`submit_job`, `job_status`, `job_logs`, `job_result`,
  `cancel_job`).
- **Auto-resume** (`--resumable`) — a reclaimed runtime is re-allocated and the job
  relaunched, restoring from its own Drive checkpoint.
- **Runtime-direct file transfer** over the Jupyter contents/files REST API — chunked
  upload, ranged streaming download (with fallback) — so real-size inputs/outputs move
  without the kernel in the data path (`gpu.upload()` / `gpu.download()`).
- **Runtime-direct Drive checkpoints** — the VM uploads model state straight to **your**
  Google Drive (resumable upload, ranged restore), no client memory/bandwidth in the path,
  wired into the lifecycle manager for automatic restore on re-assignment. A short-lived
  token is injected to a `0600` file on the VM; the ADC quota project is auto-detected.
- **Auth UX** — `colabctl auth login` (runs the gcloud ADC login with the exact scopes
  colabctl needs), `auth status` (account · scopes · Drive quota project · what to fix, via
  tokeninfo introspection), and `auth scopes` (prints the manual command).
- **Quota & spend guard** — `colabctl quota` shows the compute-unit balance, burn rate, and
  runway; a pre-allocation **spend guard** refuses to burn a zero-balance account (override
  with `--yes`), and a 412 on allocation hints at `gc`.
- **Allocation ladder** — `allocate(gpu="A100,L4,T4")` / `--gpu A100,L4,T4` tries each
  accelerator in turn.
- **Browser transport** (`-t browser`) — drives a Colab notebook through Colab's own
  (live-captured) **ColabMCP** tools via a logged-in tab; the one sanctioned path that keeps
  its runtime alive (genuine cell activity in the authenticated session). Wired into the CLI
  and SDK transport selectors.
- **Interrupt / reconnect / output cap** — `gpu.interrupt()` stops a runaway cell without
  losing the VM; the native websocket auto-reconnects; kernel stream output is bounded.
- **Drift canary** — a scheduled GitHub Action fingerprints the upstream Colab protocol and
  flags structural drift before it reaches users.

### Changed

- Repository URLs standardized to `github.com/mandipadk/colabctl`.
- Documentation refreshed (architecture, deployment, roadmap, plan) for the durable fabric
  and the resolved per-transport keep-alive story.

### Removed

- Internal planning artifacts (`SPEC.md`, `RESEARCH.md`, `DECISIONS.md`) are no longer
  shipped in the repository; the architecture overview lives in `docs/architecture.md`, the
  execution plan in `docs/plan.md`, and binding decisions in `DIRECTIVES.md`.

## [0.2.0] - 2026-06-08

Hardening release. An intensive adversarial + property-based stress sweep across
**every** subsystem (the test suite grew 271 → 555) found and fixed a class of
edge-case bugs; the public API is unchanged.

### Added

- **RunPod** IaaS backend (ephemeral GPU pods) and a **papermill-style notebook
  adapter** (`run_notebook` / `run_notebook_job` — parameter injection, cell-by-cell
  on a session or as a batch job).
- **Integration test rig** (`COLABCTL_INTEGRATION=1`) that drives the native kernel
  against a real local Jupyter server — offline-validates the kernel exec + streaming.
- **Docs:** per-backend ToS/cost matrix (`docs/backends.md`) and a deployment/operations
  guide (`docs/deployment.md`).
- **Property-based test suites** (hypothesis) covering the marshalling, parsing, quoting,
  and escaping boundaries across the SDK, transports, and backends.

### Fixed

- **Secrets (keyring):** a value literally starting with the chunk-manifest marker, or an
  account name shaped like an internal chunk key, could corrupt reads — both are now
  namespaced so neither can collide.
- **Routing:** `BackendRouter` raised a bare `KeyError` for an unregistered name in
  `order` and let duplicate names make failover re-run the same backend — now a clear
  `ConfigurationError` and de-duplicated, with omitted backends kept reachable.
- **Notebook:** invalid/keyword parameter names produced a confusing remote `SyntaxError`
  (now a clear local `ConfigurationError`), and a null/missing cell source injected the
  literal string `"None"` as code (now skipped).
- **Browser bridge:** a malformed `variant` from the frontend raised an unhandled
  `ValueError` (now defaults safely, matching accelerator/status handling), and a clean
  peer disconnect left in-flight JSON-RPC calls hanging until their timeout (now fails
  fast).
- **Native client:** a malformed assignment response (no `endpoint`) raised `KeyError` and
  a non-JSON `200` body raised `json.JSONDecodeError` — both now surface as typed
  `ColabctlError`s (`AllocationError` / `TransportError`).
- **SDK:** `ColabSession.__aexit__` could mask a user's exception when cleanup also failed;
  runtime release is now best-effort while an exception is already propagating (and still
  surfaces a release failure on a clean exit).
- **File transfer:** the base64 decoders (kernel download + `@remote` result) now use
  `validate=True`, so a corrupt payload fails loudly instead of decoding partially.

### Changed

- The runtime-lifecycle keep-alive loop now logs unexpected errors instead of swallowing
  them silently (e.g. a bug in a user checkpoint hook is now visible).
- Verified (property tests) that all backend script builders shell-quote user code so it
  cannot break out of the `python -c`/`bash -lc` argument, and that every backend's
  provider-state→`JobState` map is exhaustive over the real provider enums.

## [0.1.0] - 2026-06-08

First public release. Programmatic Google Colab control plus a multi-backend job API.

### Added

- **Transports** behind a single `TransportAdapter` contract:
  - `cli` — wraps the official `google-colab-cli` (sanctioned default), with a
    golden-tested stdout parser pinned to v0.5.7.
  - `native` — from-scratch `/tun/m/*` client + Jupyter-websocket kernel (co-primary,
    opt-in). Both **live-validated** against real Colab Pro.
- **Auth** — ADC-led providers (the Phase 0-verified path) + scope constants.
- **Secrets** — one `SecretStore` contract over keyring (chunked), an encrypted file
  (headless/CI), and an in-memory store.
- **SDK** — `ColabClient` / `ColabSession` (async, context-managed) and the `@remote`
  decorator (ship a local function to a GPU via cloudpickle).
- **CLI** — `colabctl` (Typer): `run`, `exec`, `new`, `sessions`, `status`, `stop`,
  `upload`, `download`, `keepalive`, and `job run` / `job backends`.
- **MCP server** — `colabctl-mcp` (FastMCP) exposing 9 tools (interactive + batch-job)
  so AI agents can drive Colab, Modal, and Vertex.
- **Provider abstraction** — `Backend` (submit/status/logs/result/cancel) + a
  capability-routing `BackendRouter` with infra failover, plus the **Colab**,
  **Modal** (live-validated), **Vertex AI**, **Hugging Face Jobs**, and **Kaggle**
  backends, wired into the CLI (`colabctl job …`) and MCP.
- **Browser-bridge transport** (colab-mcp model) — JSON-RPC relay over a local
  WebSocket; human-in-the-loop, needs live validation.
- **Runtime-lifecycle manager** — best-effort keep-alive ticks + proactive checkpoint
  + automatic re-assign/restore on idle reclamation.
- **Drive sync** — `DriveSync` (durable My-Drive file sync via user-OAuth) + lifecycle
  checkpoint/restore hooks.
- **Observability** — namespaced logging + a reusable `retry_async` (exponential
  backoff that never retries terminal quota/entitlement errors).
- **Spend guard** — `cap_timeout` enforces a hard billable-time ceiling on paid
  backends (wired into Modal).

### Known limitations

- The Colab RuntimeService keep-alive RPC is unusable under token auth (live-confirmed);
  long jobs rely on kernel activity + checkpoint/re-assign instead.
- Vertex stdout is in Cloud Logging (not captured); `result` returns state + a log link.
- Vertex / Hugging Face / Kaggle backends and the browser-bridge are not yet
  live-validated (no accounts in CI); their logic is unit-tested against fakes.
- Still planned: RunPod/vast.ai + hyperscaler backends, a papermill notebook adapter,
  a `jupyter_http_over_ws` integration test rig, a billing watchdog, and a docs site.

[Unreleased]: https://github.com/mandipadk/colabctl/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/mandipadk/colabctl/releases/tag/v0.1.0
