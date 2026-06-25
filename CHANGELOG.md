# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to adhere to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.4.0] - 2026-06-25

Agent-native delivery + the production-trust layer: durable jobs that agents recognize as MCP
Tasks, coded errors, one-shot tools, an audit ledger, `colabctl doctor`, structured logging,
and W&B/MLflow/Hydra integration. (Test suite 813 → 861.)

### Added

- **MCP Tasks-shaped durable jobs.** Detached-job MCP responses now carry a `taskId` (= job id)
  and a `status` in the SEP-1686 vocabulary (`working`/`completed`/`failed`/`cancelled`), so
  agents treat colabctl's VM-durable jobs as MCP Tasks — the durability primitive that survives
  the agent's context *and* the server. (Tasks-shaped now; native `task=True` deferred until the
  experimental lifecycle stabilizes.)
- **Coded errors across the MCP/SDK boundary.** Every error carries a stable `code`, a
  `category`, and a `remediation` hint (`to_dict()`), surfaced through the MCP tools — agents
  branch on the code (e.g. `QUOTA_EXCEEDED` → try another backend) instead of parsing strings.
- **One-shot agent tools** `run_once` / `run_file` — collapse allocate→run→teardown into one
  MCP call.
- **Append-only lifecycle+cost audit ledger** + `colabctl audit` — a chronological trail of
  submit/auto-resume/run events with `$` + ids, the forensic record behind the durability and
  cost-safety guarantees.
- **`colabctl doctor`** + an MCP `health_check` tool — offline preflight checks (ADC creds, the
  `colab` binary, configured backends, state-store health, agent skill) that answer "why won't
  this run" before you burn time.
- **Structured JSON logging + correlation IDs** — `configure_logging(json_logs=True)` /
  `COLABCTL_LOG_JSON=1`, a `correlation_context` that tags every log line with `job_id`/
  incarnation (greppable across a 12h cross-reassignment job), and a thin `set_event_sink` hook
  for an OpenTelemetry exporter (no otel dependency).
- **Experiment tracking — W&B + MLflow** via `@remote(track="wandb"|"mlflow")`, `job run
  --track`, and MCP `submit_job track=`. Credentials come from the secret store and are injected
  as env (never baked into pickled code); autolog is enabled on the runtime; the run is tagged
  with the job id; and the run id/URL is captured into the audit ledger (two-way lineage). Creds
  are re-resolved on auto-resume — never persisted in state.
- **`colabctl-hydra-launcher`** (separate distribution) — `hydra/launcher=colab` runs each
  Hydra `--multirun` job as a durable detached colabctl job, fanning a sweep across runtimes.

### Changed

- `JobSpec.env` is now threaded into the detached runner (previously ignored) — a general fix
  that also carries the tracking env. `StoredJob` persists the user env + tracker name (never
  credentials).

## [0.3.7] - 2026-06-25

Phase 2c finale (the spot tier) + an Agent Skill so AI agents can discover and drive colabctl.

### Added

- **Vast.ai backend** (`--backend vast`) — a bid-marketplace GPU backend (raw `/api/v0` over
  httpx, no SDK). Searches host offers filtered by reliability, picks the cheapest, and
  bid-launches a spot/interruptible instance (`--spot`, needs a `--max-price` bid; fail-closed).
  Now the cheapest A100/spot in the price table, so cost routing prefers it.
- **Spot preemption recovery** — preemption (which Vast gives *no* warning for; detected via the
  `actual_status`/`intended_status` tuple) raises a retriable error *after* tearing the host
  down, so the existing bounded router failover re-bids on the next candidate or falls back to
  on-demand for idempotent jobs. RunPod's uncleared bid takes the same path.
- **Agent Skill + `colabctl skill install`** — ships a Claude Code Agent Skill (bundled in the
  wheel) so an AI agent *discovers* colabctl and knows which commands/examples to use — the
  know-how layer that complements the MCP server's typed tools. `colabctl skill install
  [--user|--project] [--force]` / `status` / `uninstall` copies it into `~/.claude/skills/`
  (Claude Code doesn't scan site-packages); version-stamped, with an opt-out first-run hint
  (`COLABCTL_NO_SKILL_HINT`). Added `AGENTS.md` for contributors.

## [0.3.6] - 2026-06-24

Cost-aware arbitrage engine (Phase 2a + 2b + first 2c) — route to the cheapest qualifying
backend under hard, fail-closed budget caps — plus a critical out-of-box install fix.

### Added

- **Cheapest-first cost routing.** A backend-neutral price model (`colabctl.cost`: `GpuPrice`
  rows, a `PriceSource` chain, a `PriceCatalog` facade) over a hand-maintained static table.
  `job run --cheapest --allow colab,modal,runpod` orders candidates by `$/hr` and composes
  with the existing capability/failover routing.
- **Fail-closed budget caps** (OpenRouter `max_price` semantics — a guarantee, not a
  preference): `--max-price` (per-job `$/hr` ceiling) and `--budget` (cumulative USD cap read
  from the persisted ledger, so a restart/auto-resume can't reset spend and slip past it). If
  nothing qualifies, it **refuses to launch** — never silently picks a pricier backend.
- **Cross-backend USD spend ledger** + `colabctl spend`; each cost-routed run records an
  estimated `SpendRecord`, so spend is auditable and the cumulative cap has live data.
- **`colabctl cost [--gpu A100] [--spot] [--live]`** — dry-run price estimator, cheapest-first.
  `--live` overlays a cached, plausibility-guarded **ComputePrices** market feed (the static
  table stays the deterministic, offline-safe routing floor and the fallback).
- **`colabctl spot-risk`** — per-accelerator spot interruption-rate + savings from AWS's free
  Spot Advisor feed (directional reference; H100 <5% … T4 >20%).
- **RunPod spot tier** — `--spot` bids via the GraphQL `podRentInterruptable` path (the SDK
  has no bid param) with a fail-closed max bid; advertises `supports_spot`/`prepaid_wallet`/
  `preempt_notice_seconds`.

### Fixed

- **`colabctl[all]` now works out of the box.** The default `cli` transport drives Google's
  `colab` binary, but colabctl never shipped it — a fresh `uv tool install "colabctl[all]"`
  couldn't run until you *separately* installed google-colab-cli. Now `google-colab-cli` is
  bundled in the `cli`/`all` extras, and the transport resolves `colab` from colabctl's own
  environment (uv-tool installs a dependency's script into the venv but not onto PATH). A
  genuinely missing binary now yields an actionable error pointing at the fix + the binary-free
  `-t native`/`-t browser` transports.

### Changed

- **Requires Python 3.12+** (google-colab-cli's floor). Live price feeds degrade
  cached → static so a feed outage never breaks routing; aggregator unit-error rows (e.g. a
  per-minute price mislabeled hourly) are dropped by per-accelerator plausibility floors.

## [0.3.5] - 2026-06-24

Durability + credibility release. A native **headless keep-alive** that actually works, the
failover/auto-resume/`@remote` claims made real and bulletproof, crash-safe checkpoints, and
the notebook runner finally reachable. (Test suite 708 → 758.)

### Added

- **Headless token-auth keep-alive (native).** The native transport keeps a runtime alive
  with the tunnel ping (`GET /tun/m/<endpoint>/keep-alive/?authuser=0` + `X-Colab-Tunnel:
  Google`, the google-colab-cli recipe) — no browser tab, no kernel needed. Live-validated
  to hold a runtime **100+ minutes past idle** with zero activity; `Capabilities.keepalive`
  is now `True`. (Colab's hard 12/24h cap still applies, so durable long jobs rely on
  checkpoint + auto-resume regardless.)
- **Opt-in cross-backend failover** — `colabctl job run --backend colab --allow colab,modal,
  vertex` (and MCP `run_job(allow=…)`) route through the capability router, so a Colab outage
  degrades to the next backend. (Wires what was previously dead code.)
- **`colabctl notebook run nb.ipynb --param k=v --gpu T4 [--detach] [--out executed.ipynb]`**
  and an MCP `run_notebook` tool — papermill-style parameterized execution on a remote GPU,
  emitting an executed `.ipynb` artifact. (The runner existed but was unreachable.)
- **`@remote(requirements=[…], env={…})`** — declare pip deps + env on the runtime; cloudpickle
  is pinned to the host version (fixes the by-value-pickle skew). Remote exceptions now
  **re-raise as native Python objects** locally, with the remote traceback attached.
- **Durable-job observability**: a state-transition event log + `colabctl job history`;
  `colabctl job gc` (reconcile dead jobs to FAILED, prune terminal records past a TTL) and
  `colabctl job rm`.
- **`colabctl update`** — self-upgrade to the latest PyPI release (auto-detects uv-tool vs pip).
- Friendly missing-extra errors on a bare install; the daily drift **canary** now alerts
  (auto-files a GitHub issue) and asserts baseline integrity.

### Fixed

- **Bounded auto-resume.** A flapping runtime no longer re-allocates paid GPUs forever — a
  hard incarnation cap + exponential backoff + a terminal `FAILED` state (the worst cost
  footgun, closed).
- **Crash-safe checkpoint versioning.** Runtime-direct Drive uploads are now two-phase +
  end-to-end MD5-verified (temp blob → verify → promote), so a crash or a corrupt upload can
  never destroy the last-good checkpoint (was overwrite-in-place with a size-only check).
- **Process liveness.** A runner killed without writing an exit code (OOM/SIGKILL) now
  resolves to `FAILED` instead of lying `RUNNING` forever.
- **Atomic, locked secret writes.** `EncryptedFileSecretStore` was a bare `write_text` that
  corrupted *all* secrets on a crash; it now writes via temp-file + `fsync` + `os.replace`
  under a lock (extracted to a shared `colabctl.fsutil`).
- **Log stitching.** Auto-resume no longer silently resets the log to zero — `logs`/`result`
  show a continuous, attributable view with incarnation boundary markers.
- **Streaming `run`/`exec`** so long interactive runs no longer look hung.
- Ship `py.typed` (the `Typing :: Typed` classifier was unbacked); single-source `__version__`.

### Changed

- `Development Status` classifier `Pre-Alpha` → `Alpha`; README/ROADMAP/docs refreshed for
  the working keep-alive and the durable fabric.

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
